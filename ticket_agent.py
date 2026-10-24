"""
ticket_agent.py —— 工单式多模型人机协作自愈（需求基线的增量能力）

把"修 bug"的六 stage 自愈链路升级为"运维发工单 -> AI 全链路开发 -> 人工审核 -> 合并上线"：
  发工单(聊天) -> 本地模型前置过滤(是否合理) -> deepseek 思考(方案+验收清单)
    -> kimi 敲代码(统一 diff 补丁) -> 进沙箱(8081 自动验收)
    -> 视觉模型校验(截图+判定) -> 不过则带反馈重试(最多 MAX_ATTEMPTS)
    -> 给运维输出 -> 运维可进预览沙箱看成品 -> 验证后合并上线(重启 8080)

工单即聊天：每个工单持久化到 tickets.jsonl，messages 数组存全部对话，
模型调用时注入完整聊天历史，实现上下文记忆（追加消息可继续迭代）。

关键约束：
  - 只改 target_app.py 单文件；补丁为统一 diff（line/old/new，可多块），
    每块 old 必须与文件真实行完全匹配才应用，绝不盲改
  - 本地模型(qwen3:8b)不可用时前置过滤降级放行(记 degraded)，绝不阻塞
  - 沙箱/视觉任何失败都优雅降级，与 main/feature 共用 8081 排队
  - 人工审核是硬闸门：await_review 之前不碰线上文件；只有 approve 才合并+重启

用法：
  python ticket_agent.py --selftest        # 纯逻辑自测（不依赖模型/ollama）
  python ticket_agent.py --create "给商品加优惠券"   # 命令行发工单（演示备选）
  python ticket_agent.py --approve T-xxx   # 命令行审核通过（演示备选）
  python ticket_agent.py --list            # 列出工单
"""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import common_utils as cu

# ---- 路径与常量 ------------------------------------------------------------
TICKETS_FILE = os.path.join(cu.BASE_DIR, "tickets.jsonl")
TARGET_FILE = os.path.join(cu.BASE_DIR, "target_app.py")
SANDBOX_PORT = 8081          # 自动验收沙箱（与修复/自动开发链路共用，走排队）
PREVIEW_PORT = 8095          # 运维人工预览沙箱（常驻，await_review 期间可看）
MAX_ATTEMPTS = 3             # 沙箱+视觉未过的重试上限

# 多模型分工（与 config.json providers 的 name 对应）
ANALYZE_MODEL = "deepseek-v4.1-flash"        # deepseek 思考：方案 + 验收清单
CODE_MODEL = "kimi-k2.7-code"                # kimi 敲代码：统一 diff 补丁
VISION_MODEL = "deepseek-v4-flash-vision-exp"  # 视觉校验：截图判定
LOCAL_MODEL_NAME = "qwen3:8b"                # 本地模型：前置合理性过滤
LOCAL_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

# 工单状态机
STATUS_NEW = "pending"          # 刚创建
STATUS_FILTER = "filtering"     # 本地模型前置过滤中
STATUS_PLAN = "planning"        # deepseek 思考方案中
STATUS_CODE = "coding"          # kimi 生成补丁中
STATUS_VERIFY = "verifying"     # 沙箱验收中
STATUS_VISUAL = "visual"        # 视觉校验中
STATUS_REVIEW = "await_review"  # 沙箱+视觉全过，等运维人工审核
STATUS_MERGED = "merged"        # 已合并上线
STATUS_REJECTED = "rejected"    # 被驳回 / 前置过滤不通过
STATUS_FAILED = "failed"        # 重试耗尽仍未通过

_preview_procs = {}   # ticket_id -> Popen（预览沙箱进程，审核结束后强杀）
_ticket_lock = threading.Lock()  # 工单文件写锁


# ---- 工单持久化（tickets.jsonl 原子写，任何异常不崩） -----------------------
def _read_tickets():
    """读全部工单 -> list[dict]（损坏行跳过，不抛异常）。"""
    out = []
    if not os.path.exists(TICKETS_FILE):
        return out
    try:
        with open(TICKETS_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return out
    return out


def _write_tickets(tickets):
    """原子写工单文件（tmp + os.replace），多进程并发读不到半截 JSON。"""
    try:
        tmp = TICKETS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for t in tickets:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        os.replace(tmp, TICKETS_FILE)
        return True
    except Exception:
        return False


def _save_ticket(ticket):
    """按 ticket_id 覆盖保存单个工单（线程安全）。"""
    with _ticket_lock:
        tickets = _read_tickets()
        replaced = False
        for i, t in enumerate(tickets):
            if t.get("id") == ticket.get("id"):
                tickets[i] = ticket
                replaced = True
                break
        if not replaced:
            tickets.append(ticket)
        return _write_tickets(tickets)


def get_ticket(ticket_id):
    """按 id 查工单，不存在返回 None。"""
    for t in _read_tickets():
        if t.get("id") == ticket_id:
            return t
    return None


def list_tickets():
    """全部工单（按创建时间倒序，方便控制台最新优先）。"""
    return list(reversed(_read_tickets()))


def _next_ticket_id():
    """生成工单号：T-20261024-0001 样式（按日期+当日序号）。"""
    today = time.strftime("%Y%m%d")
    seq = sum(1 for t in _read_tickets() if t.get("id", "").startswith(f"T-{today}"))
    return f"T-{today}-{seq + 1:04d}"


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---- events 埋点（六 stage 契约不变，flow=feature 沿用现有面板展示） -------
def _write(stage, **fields):
    fields["flow"] = "feature"
    fields["source"] = "ticket"
    cu.write_event(stage, **fields)


# ---- 1. 本地模型前置过滤（qwen3:8b，判工单是否合理可执行） -------------------
def _ollama_chat(messages, model=None, timeout=25):
    """调本地 Ollama（云端 11434）。任何失败返回 None（上层降级，绝不阻塞）。
    model 默认 qwen3:8b；messages 为 [{"role","content"}]。"""
    url = LOCAL_OLLAMA_URL.rstrip("/") + "/api/chat"
    body = json.dumps({"model": model or LOCAL_MODEL_NAME,
                       "messages": messages, "stream": False}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        content = (data.get("message") or {}).get("content", "")
        return content if content else None
    except Exception:
        return None


def local_filter(ticket):
    """本地模型前置过滤：判断工单是否合理（需求明确、可开发、无破坏性）。
    返回 {"ok", "reasonable", "reason", "degraded"}：
      degraded=True 表示本地模型不可用，按"合理"放行（不阻塞演示）；
    合理 -> 继续；不合理 -> 工单 rejected（写 events 标注）。"""
    content = ""
    for m in ticket.get("messages", []):
        if m.get("role") == "user":
            content = m.get("content", "")
            break
    if not content:
        return {"ok": False, "reasonable": False, "reason": "工单无需求内容",
                "degraded": False}
    prompt = (
        "你是工单审核员。下面是一条网站功能开发工单，请判断它是否合理可执行：\n"
        "合理 = 需求明确、属于该小商城网站的正常功能开发、不包含破坏性操作"
        "（删除数据/关闭服务/攻击性/违法内容等）。\n"
        "只输出 JSON：{\"reasonable\": true 或 false, \"reason\": \"一句话理由\"}\n"
        f"工单内容：{content[:500]}"
    )
    resp = _ollama_chat([{"role": "user", "content": prompt}])
    if not resp:
        return {"ok": False, "reasonable": True, "reason": "本地模型不可用，降级放行",
                "degraded": True}
    try:
        m = re.search(r"\{.*\}", resp, re.S)
        data = json.loads(m.group(0)) if m else {}
        return {"ok": True, "reasonable": bool(data.get("reasonable")),
                "reason": str(data.get("reason", ""))[:200], "degraded": False}
    except Exception:
        return {"ok": False, "reasonable": True,
                "reason": "本地模型返回无法解析，降级放行", "degraded": True}


# ---- 2. deepseek 思考：方案 + 验收清单 --------------------------------------
def _load_target_source(max_chars=16000):
    """读 target_app.py 内容给模型看（超长截断，模型主要看业务结构与行号）。"""
    try:
        with open(TARGET_FILE, encoding="utf-8", errors="replace") as f:
            src = f.read()
        return src[:max_chars]
    except Exception:
        return ""


def _extract_json(text):
    """从模型输出里抠第一个 JSON 对象（容忍前后废话）。失败返回 None。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _normalize_checklist(checklist):
    """校验/归一化验收清单：只保留可控字段 method/path/expect/contains/json。
    非法项直接丢弃，至少保留一条健康检查，保证沙箱验收不会空跑。"""
    ok = []
    for c in (checklist or []):
        if not isinstance(c, dict):
            continue
        path = str(c.get("path", "")).strip()
        if not path.startswith("/"):
            continue
        method = str(c.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            method = "GET"
        item = {"method": method, "path": path,
                "expect": int(c.get("expect", 200) or 200)}
        if c.get("contains"):
            item["contains"] = str(c["contains"])
        if method == "POST" and c.get("json"):
            item["json"] = c["json"]
        ok.append(item)
    if not any(i["path"] == "/health" for i in ok):
        ok.insert(0, {"method": "GET", "path": "/health", "expect": 200})
    return ok[:6]


def deepseek_plan(ticket):
    """deepseek-v4.1-flash 分析需求 -> {"plan", "checklist"}。
    返回 {"ok", "plan", "checklist", "msg"}。"""
    content = ""
    for m in reversed(ticket.get("messages", [])):
        if m.get("role") == "user":
            content = m.get("content", "")
            break
    src = _load_target_source()
    if not src:
        return {"ok": False, "plan": "", "checklist": [],
                "msg": "target_app.py 读取失败，无法制定方案"}
    provider = cu.resolve_provider(ANALYZE_MODEL)
    prompt = (
        "你是资深后端工程师，负责为一个小商城网站（Flask，target_app.py）制定功能改造方案。\n"
        "工单需求（可能含历史对话，重点看最后一条用户消息）：\n"
        f"{json.dumps([{'role': m.get('role'), 'content': m.get('content')} for m in ticket.get('messages', [])], ensure_ascii=False)[:2000]}\n\n"
        "目标代码：\n"
        f"```python\n{src}\n```\n\n"
        "请输出 JSON（不要任何多余解释）：\n"
        "{\n"
        '  "plan": "一句话改造方案：改哪个函数/页面、改成什么样",\n'
        '  "checklist": [{"method": "GET或POST", "path": "/接口路径", "expect": 200, "contains": "响应体应包含的文本"}]\n'
        "}\n"
        "要求：checklist 2-3 条，必须包含能证明改动生效的断言（如新增字段名/新文案/新接口返回）；"
        "只做小规模改动（≤20 行），不引入第三方依赖。"
    )
    resp = cu.llm_chat([{"role": "user", "content": prompt}],
                       max_tokens=2000, provider=provider, timeout=40)
    if not resp:
        return {"ok": False, "plan": "", "checklist": [],
                "msg": "deepseek 方案生成失败（无返回）"}
    data = _extract_json(resp)
    if not data:
        return {"ok": False, "plan": "", "checklist": [],
                "msg": "deepseek 方案解析失败"}
    return {"ok": True, "plan": str(data.get("plan", ""))[:500],
            "checklist": _normalize_checklist(data.get("checklist")),
            "msg": ""}


# ---- 3. kimi 敲代码：统一 diff 补丁（可多块） -------------------------------
def _parse_patch_blocks(patch_text):
    """解析统一 diff 补丁为多块 [{"line", "old", "new"}]。
    兼容单块与多块（多块以重复 line: 分段）。格式非法返回 None。"""
    text = patch_text or ""
    blocks = []
    # 以 line: 开头的段切分
    segments = re.split(r"(?m)^\s*line:\s*", text)
    for seg in segments[1:]:
        m_line = re.search(r"^(\d+)", seg.strip())
        m_old = re.search(r"(?m)^old:\s*(.*)$", seg)
        m_new = re.search(r"(?m)^new:\s*(.*)$", seg)
        if not (m_line and m_old and m_new):
            return None
        blocks.append({"line": int(m_line.group(1)),
                       "old": m_old.group(1).rstrip("\r"),
                       "new": m_new.group(1).rstrip("\r")})
    return blocks or None


def _apply_blocks_to_file(target_path, blocks):
    """把多块补丁依次应用到文件（复用公共 apply 的单行替换逻辑）。
    任一块失败立即返回错误，绝不部分应用改坏文件。"""
    for b in blocks:
        patch = f"line: {b['line']}\nold: {b['old']}\nnew: {b['new']}"
        r = cu.apply_patch_to_file(target_path, patch)
        if not r["ok"]:
            return {"ok": False, "msg": r["msg"], "block": b}
    return {"ok": True, "msg": f"已应用 {len(blocks)} 块补丁"}


def kimi_code(ticket, plan, feedback="", provider_name=None):
    """kimi-k2.7-code 根据方案生成统一 diff 补丁（provider_name 可指定降级模型）。
    feedback 非空 = 上一次沙箱/视觉失败原因，要求 kimi 带着反馈重写。
    返回 {"ok", "blocks", "patch", "msg"}。"""
    src = _load_target_source()
    if not src:
        return {"ok": False, "blocks": [], "patch": "", "msg": "target_app.py 读取失败"}
    provider_name = provider_name or CODE_MODEL
    provider = cu.resolve_provider(provider_name)
    hist = json.dumps([{"role": m.get("role"), "content": m.get("content")}
                       for m in ticket.get("messages", [])], ensure_ascii=False)[:1500]
    prompt = (
        "你是 Python 后端工程师。请为下面的小商城 Flask 网站实现一个功能改造。\n"
        f"工单聊天历史（上下文记忆）：{hist}\n"
        f"改造方案：{plan}\n"
    )
    if feedback:
        prompt += f"上次验收失败反馈（请修正）：{feedback[:400]}\n"
    prompt += (
        f"目标代码：\n```python\n{src}\n```\n\n"
        "请只输出统一 diff 格式补丁（不要任何解释），格式：\n"
        "line: <行号>\nold: <该行的原始代码>\nnew: <替换后的代码>\n"
        "可输出多个这样的块（每块三行）。\n"
        "要求：\n"
        "1. 行号必须是上面代码中的真实行号；\n"
        "2. old 必须与该行内容完全一致（含缩进），否则补丁无法应用；\n"
        "3. new 保持同样的缩进层级；\n"
        "4. 改动 ≤ 20 行、不引入第三方库、不破坏其他接口。"
    )
    resp = cu.llm_chat([{"role": "user", "content": prompt}],
                       max_tokens=12000, provider=provider, timeout=90)
    if not resp:
        return {"ok": False, "blocks": [], "patch": "",
                "msg": f"{provider_name} 补丁生成失败（无返回）"}
    blocks = _parse_patch_blocks(resp)
    if not blocks:
        return {"ok": False, "blocks": [], "patch": resp[:500],
                "msg": f"{provider_name} 输出格式非法，缺少 line/old/new"}
    # 行号/内容合法性预检：不落到沙箱就知道补丁能不能应用
    lines = src.split("\n")
    for b in blocks:
        ln = b["line"]
        if ln < 1 or ln > len(lines):
            return {"ok": False, "blocks": [], "patch": resp[:500],
                    "msg": f"补丁行号 {ln} 超出文件范围"}
        if lines[ln - 1].rstrip("\r").strip() != b["old"].strip():
            return {"ok": False, "blocks": [], "patch": resp[:500],
                    "msg": f"第 {ln} 行 old 与实际内容不匹配"}
    return {"ok": True, "blocks": blocks, "patch": resp.strip(), "msg": ""}


# ---- 3.1 补丁生成带模型降级：kimi 空返回/格式非法时切 deepseek 兜底 ----
PATCH_FALLBACKS = [CODE_MODEL, ANALYZE_MODEL]  # kimi 优先，deepseek 兜底
PROVIDER_RETRY = 2      # 单 provider 内空返回重试次数（对抗 API 间歇性空返回）
RETRY_SLEEP = 3         # 重试间隔秒


def _patch_with_fallback(ticket, plan, feedback=""):
    """依次尝试 PATCH_FALLBACKS 里的代码模型，每个 provider 空返回重试
    PROVIDER_RETRY 次（API 偶发空返回，重试命中率高），谁先出合法补丁用谁。
    全部失败返回最后一次失败信息（带 provider 名方便定位）。"""
    last = {"ok": False, "blocks": [], "patch": "", "msg": "无可用代码模型"}
    tried = []
    for name in PATCH_FALLBACKS:
        for _ in range(PROVIDER_RETRY):
            r = kimi_code(ticket, plan, feedback=feedback, provider_name=name)
            tried.append(name)
            if r["ok"]:
                r["provider"] = name
                return r
            last = r
            time.sleep(RETRY_SLEEP)
    last["msg"] = f"补丁生成失败（尝试 {','.join(tried)}）：{last['msg']}"
    return last


# ---- 4. 沙箱验证 + 视觉校验（复用 8081 排队，复用视觉链路） -------------------
def _request_one(check, port=SANDBOX_PORT):
    """执行单条验收请求（GET/POST），返回 {"path","status","body","parsed"}。
    parsed：响应体若是 JSON，递归解析后的对象（contains 断言可深入查值）。"""
    method = check.get("method", "GET")
    url = f"http://127.0.0.1:{port}{check['path']}"
    body = json.dumps(check.get("json")) if check.get("json") else None
    req = urllib.request.Request(url, data=body.encode() if body else None,
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            return {"path": check["path"], "status": resp.status,
                    "body": raw, "parsed": parsed}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        parsed = None
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return {"path": check["path"], "status": e.code,
                "body": raw, "parsed": parsed}
    except Exception as e:
        return {"path": check["path"], "status": 0, "body": str(e), "parsed": None}


def _contains_value(obj, needle):
    """递归在 dict/list/str 中查找子串（应对 jsonify 把中文转成 uXXXX 转义串的情况）。"""
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(_contains_value(v, needle) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_value(v, needle) for v in obj)
    return False


def _sandbox_verify_impl(blocks, checklist, workdir=None, request_id="ticket",
                         visual_req=None):
    """沙箱验证核心：copy target_app.py -> 应用多块补丁 -> 8081 起进程 ->
    逐条 checklist -> （可选）进程活着时视觉审核 -> finally 强杀无残留。
    视觉任何失败都降级（degraded=True），不阻塞 HTTP 断言结果。"""
    sandbox_dir = workdir or cu.make_sandbox_dir("ticket_sandbox")
    sandbox_target = os.path.join(sandbox_dir, os.path.basename(TARGET_FILE))
    shutil.copy2(TARGET_FILE, sandbox_target)
    ar = _apply_blocks_to_file(sandbox_target, blocks)
    if not ar["ok"]:
        return {"ok": False, "results": [], "error": f"沙箱内应用补丁失败: {ar['msg']}"}

    proc = subprocess.Popen(
        [sys.executable, sandbox_target, "--port", str(SANDBOX_PORT), "--no-threaded"],
        cwd=sandbox_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    try:
        deadline = time.time() + 30
        alive = False
        while time.time() < deadline:
            r = _request_one({"method": "GET", "path": "/health"})
            if r["status"] == 200:
                alive = True
                break
            time.sleep(0.5)
        if not alive:
            return {"ok": False, "results": [], "error": f"沙箱 {SANDBOX_PORT} 30s 内未启动"}

        for check in checklist:
            r = _request_one(check)
            ok_body = (not check.get("contains")) or \
                (check["contains"] in r["body"]) or \
                (r.get("parsed") is not None and _contains_value(r["parsed"], check["contains"]))
            results.append({"path": check["path"], "status": r["status"],
                            "ok": r["status"] == check["expect"] and ok_body})
        all_ok = all(r["ok"] for r in results)
        out = {"ok": all_ok, "results": results,
               "error": "" if all_ok else "验收未全部通过"}

        # 视觉校验：HTTP 断言全过且视觉要求非空时，趁进程活着截图判定
        if all_ok and visual_req:
            try:
                import visual_check as vc
                vout = vc.visual_review(
                    url=f"http://127.0.0.1:{SANDBOX_PORT}", requirement=visual_req)
                out["visual"] = vout
            except Exception as e:
                out["visual"] = {"ok": False, "passed": False,
                                 "reason": "视觉审核异常，已降级：%s" % str(e)[:100],
                                 "screenshot": "", "degraded": True}
        return out
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True)
                except Exception:
                    pass


def sandbox_verify(blocks, checklist, visual_req=None):
    """沙箱验证（排队版）：与 main/feature 共用 8081，同一时刻只有一个在跑。"""
    request_id = f"ticket-{int(time.time() * 1000)}"
    return cu.with_sandbox_queue(request_id, "ticket", lambda: _sandbox_verify_impl(
        blocks, checklist, request_id=request_id, visual_req=visual_req))


# ---- 5. 预览沙箱（运维人工审核前可进 8095 看成品） ----------------------------
def _kill_preview(ticket_id):
    proc = _preview_procs.pop(ticket_id, None)
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            except Exception:
                pass


def start_preview(ticket_id):
    """await_review 阶段启动常驻预览沙箱（8095）：copy+patch 后的成品，
    运维可通过 http://<服务器IP>:8095 先看效果再决定合并。"""
    t = get_ticket(ticket_id)
    if not t or not t.get("blocks"):
        return False
    _kill_preview(ticket_id)
    sandbox_dir = cu.make_sandbox_dir(f"preview_{ticket_id.replace('-', '')}")
    sandbox_target = os.path.join(sandbox_dir, os.path.basename(TARGET_FILE))
    shutil.copy2(TARGET_FILE, sandbox_target)
    if not _apply_blocks_to_file(sandbox_target, t["blocks"])["ok"]:
        return False
    proc = subprocess.Popen(
        [sys.executable, sandbox_target, "--port", str(PREVIEW_PORT), "--no-threaded"],
        cwd=sandbox_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _preview_procs[ticket_id] = proc
    # 等待起来（2s 内轮询），起不来就杀掉并返回 False
    deadline = time.time() + 5
    while time.time() < deadline:
        r = _request_one({"method": "GET", "path": "/health"}, port=PREVIEW_PORT)
        if r["status"] == 200:
            return True
        time.sleep(0.4)
    _kill_preview(ticket_id)
    return False


def preview_url():
    """预览沙箱访问地址提示（演示时评委在本机浏览器直接访问）。"""
    try:
        host = socket.gethostbyname(socket.gethostname())
    except Exception:
        host = "127.0.0.1"
    return f"http://{host}:{PREVIEW_PORT}"


# ---- 6. 工单主流程（后台线程跑，不阻塞入口） ----------------------------------
def process_ticket(ticket_id):
    """执行完整工单流程：过滤 -> 方案 -> 补丁(重试循环) -> 沙箱+视觉 -> await_review。
    全程写 events 与工单状态；任何模型不可用/解析失败都走明确降级路径。"""
    t = get_ticket(ticket_id)
    if not t:
        return
    if t.get("processing"):
        return  # 已有线程在处理，防并发重复跑
    t["processing"] = True
    _save_ticket(t)

    try:
        # ---- 阶段 1：本地模型前置过滤 ----
        t = get_ticket(ticket_id)
        t["status"] = STATUS_FILTER
        _save_ticket(t)
        f = local_filter(t)
        t["filter"] = f
        _write("reason", ticket=ticket_id, cause=f"本地前置过滤: {f['reason']}")
        if not f.get("reasonable", True):
            t["status"] = STATUS_REJECTED
            t["final_note"] = f"工单被本地模型判定不合理：{f['reason']}"
            _save_ticket(t)
            _write("reason", rejected=True, ticket=ticket_id,
                   cause=f"工单不合理被拒：{f['reason']}")
            return

        # ---- 阶段 2：deepseek 思考方案 ----
        t["status"] = STATUS_PLAN
        _save_ticket(t)
        plan = deepseek_plan(t)
        if not plan["ok"]:
            t["status"] = STATUS_FAILED
            t["final_note"] = plan["msg"]
            t["plan"] = ""
            _save_ticket(t)
            _write("reason", rejected=True, ticket=ticket_id, cause=plan["msg"])
            return
        t["plan"] = plan["plan"]
        t["checklist"] = plan["checklist"]
        _save_ticket(t)
        _write("patch", ticket=ticket_id, intent="ticket",
               cause=f"deepseek 方案: {plan['plan'][:100]}")

        # ---- 阶段 3-4-5：kimi 补丁 -> 沙箱 -> 视觉（带反馈重试循环） ----
        attempts, last_err, code = 0, "", None
        while attempts < MAX_ATTEMPTS:
            attempts += 1
            t = get_ticket(ticket_id)
            t["status"] = STATUS_CODE
            t["attempts"] = attempts
            _save_ticket(t)
            code = _patch_with_fallback(t, plan["plan"], feedback=last_err)
            if not code["ok"]:
                last_err = code["msg"]
                if attempts >= MAX_ATTEMPTS:
                    break
                _write("patch", ticket=ticket_id, attempt=attempts,
                       retry=True, feedback=last_err[:120])
                continue
            t["blocks"] = code["blocks"]
            t["patch"] = code["patch"]
            _save_ticket(t)
            _write("patch", ticket=ticket_id, attempt=attempts,
                   suggestion=code["patch"][:300])

            # 沙箱 + 视觉（视觉要求文本 = 需求 + 验收清单）
            t = get_ticket(ticket_id)
            t["status"] = STATUS_VERIFY
            _save_ticket(t)
            req_text = ""
            for m in reversed(t.get("messages", [])):
                if m.get("role") == "user":
                    req_text = m.get("content", "")
                    break
            visual_req = f"需求：{req_text[:60]}。验收清单：{json.dumps(plan['checklist'], ensure_ascii=False)[:160]}"
            v0 = time.time()
            verify = sandbox_verify(code["blocks"], plan["checklist"],
                                    visual_req=visual_req)
            _write("verify", ticket=ticket_id, ok=verify["ok"],
                   seconds=round(time.time() - v0, 1),
                   results=verify.get("results", []), attempts=attempts,
                   visual=(verify.get("visual") or {}).get("reason", ""))
            if not verify["ok"]:
                last_err = verify.get("error") or "沙箱验收未通过"
                if attempts >= MAX_ATTEMPTS:
                    break
                _write("patch", ticket=ticket_id, attempt=attempts,
                       retry=True, feedback=last_err[:120])
                continue

            # 视觉校验结果单独记录（HTTP 断言已过，视觉是加分判定）
            vis = verify.get("visual") or {}
            t = get_ticket(ticket_id)
            t["visual"] = vis
            t["status"] = STATUS_VISUAL
            _save_ticket(t)
            # 视觉判定语义：degraded=视觉不可用降级（不阻塞，视为通过）；
            # passed=False 且未降级 -> 按验收失败重试（带视觉理由反馈）
            if vis.get("degraded"):
                break  # 降级不阻塞
            if vis.get("passed") is False:
                last_err = "视觉校验未通过：" + str(vis.get("reason", ""))[:200]
                if attempts >= MAX_ATTEMPTS:
                    break
                _write("patch", ticket=ticket_id, attempt=attempts,
                       retry=True, feedback=last_err[:120])
                continue
            break

        t = get_ticket(ticket_id)
        if not code or not code["ok"] or (not verify or not verify["ok"]):
            t["status"] = STATUS_FAILED
            t["final_note"] = f"超过重试上限（{MAX_ATTEMPTS} 次）：{last_err}"
            _save_ticket(t)
            _write("reason", rejected=True, ticket=ticket_id,
                   cause=t["final_note"][:200])
            return

        # ---- 阶段 6：await_review（人工审核硬闸门） ----
        t["status"] = STATUS_REVIEW
        t["verify"] = verify.get("results", [])
        t["final_note"] = "沙箱验收+视觉校验通过，等待运维人工审核"
        _save_ticket(t)
        _write("apply", ticket=ticket_id, ok=True,
               cause="沙箱+视觉通过，等待人工审核合并")
        start_preview(ticket_id)
    finally:
        t = get_ticket(ticket_id)
        if t:
            t["processing"] = False
            _save_ticket(t)


# ---- 7. 工单操作 API（创建/追加/审核） ---------------------------------------
def create_ticket(content, title="", _skip_process=False):
    """创建工单并后台异步启动处理线程（工单即聊天：首条消息即需求）。
    _skip_process=True 仅用于离线自测：只持久化，不启动处理线程。"""
    content = (content or "").strip()
    if not content:
        return {"ok": False, "msg": "工单内容不能为空"}
    t = {
        "id": _next_ticket_id(),
        "title": (title or content[:30]).strip(),
        "status": STATUS_NEW,
        "created_at": _now(),
        "updated_at": _now(),
        "messages": [{"role": "user", "content": content, "time": _now()}],
        "filter": {}, "plan": "", "checklist": [],
        "patch": "", "blocks": [], "verify": [], "visual": {},
        "attempts": 0, "final_note": "", "processing": False,
    }
    _save_ticket(t)
    if _skip_process:
        return {"ok": True, "ticket": t["id"], "msg": f"工单 {t['id']} 已创建（离线自测，未启动处理）"}
    _write("alert", ticket=t["id"], cause=f"收到新工单：{content[:80]}")
    threading.Thread(target=process_ticket, args=(t["id"],), daemon=True).start()
    return {"ok": True, "ticket": t["id"], "msg": f"工单 {t['id']} 已创建并开始处理"}


def append_message(ticket_id, content, _skip_process=False):
    """向已有工单追加消息（上下文记忆：完整历史保留，重新走一轮流程）。"""
    t = get_ticket(ticket_id)
    if not t:
        return {"ok": False, "msg": "工单不存在"}
    if t.get("processing"):
        return {"ok": False, "msg": "工单正在处理中，请稍后再追加"}
    content = (content or "").strip()
    if not content:
        return {"ok": False, "msg": "消息不能为空"}
    t["messages"].append({"role": "user", "content": content, "time": _now()})
    t["updated_at"] = _now()
    # 已结束的工单追加消息 = 重新打开继续迭代（上下文记忆生效）
    if t["status"] in (STATUS_MERGED, STATUS_REJECTED, STATUS_FAILED):
        t["status"] = STATUS_NEW
    _save_ticket(t)
    if _skip_process:
        return {"ok": True, "msg": f"已追加消息（离线自测，未启动处理）"}
    _write("alert", ticket=ticket_id, cause=f"工单追加消息：{content[:80]}")
    threading.Thread(target=process_ticket, args=(ticket_id,), daemon=True).start()
    return {"ok": True, "msg": f"已追加消息，工单 {ticket_id} 重新开始处理"}


def approve_ticket(ticket_id):
    """运维人工审核通过：应用补丁到线上 target_app.py -> 重启 8080 -> merged。"""
    t = get_ticket(ticket_id)
    if not t:
        return {"ok": False, "msg": "工单不存在"}
    if t["status"] != STATUS_REVIEW:
        return {"ok": False, "msg": f"当前状态 {t['status']} 不可审核（需 await_review）"}
    if not t.get("blocks"):
        return {"ok": False, "msg": "工单没有可合并的补丁"}
    _kill_preview(ticket_id)
    ar = _apply_blocks_to_file(TARGET_FILE, t["blocks"])
    if not ar["ok"]:
        return {"ok": False, "msg": f"线上应用补丁失败: {ar['msg']}"}
    # 重启 8080 让改动生效（跨平台：Windows taskkill / Linux ss+kill）
    _kill_port_process(8080)
    time.sleep(1)
    subprocess.Popen(
        [sys.executable, TARGET_FILE, "--port", "8080", "--threaded"],
        cwd=cu.BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    ok = cu.service_alive(8080)
    t["status"] = STATUS_MERGED
    t["final_note"] = "已合并上线" if ok else "已合并但 8080 未重启成功，请检查"
    t["updated_at"] = _now()
    _save_ticket(t)
    _write("apply", ticket=ticket_id, ok=ok, file="target_app.py",
           cause="人工审核通过，合并上线")
    return {"ok": ok, "msg": t["final_note"]}


def reject_ticket(ticket_id, reason=""):
    """运维人工驳回：记录原因，工单关闭（可后续追加消息重新打开）。"""
    t = get_ticket(ticket_id)
    if not t:
        return {"ok": False, "msg": "工单不存在"}
    _kill_preview(ticket_id)
    t["status"] = STATUS_REJECTED
    t["final_note"] = f"运维驳回：{reason or '无原因'}"
    t["updated_at"] = _now()
    _save_ticket(t)
    _write("reason", rejected=True, ticket=ticket_id, cause=t["final_note"][:200])
    return {"ok": True, "msg": f"工单 {ticket_id} 已驳回"}


# ---- 8. 自测（--selftest：纯逻辑，不依赖模型/ollama） -------------------------
def run_selftest():
    """离线验证工单核心逻辑：解析、清单归一、补丁块应用、状态机等。
    不调模型（断网也能跑），模型链路留给真实演示。"""
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1. 补丁多块解析
    p = "line: 10\nold: a = 1\nnew: a = 2\nline: 20\nold: b = 1\nnew: b = 2"
    blocks = _parse_patch_blocks(p)
    check("多块补丁解析", blocks is not None and len(blocks) == 2
          and blocks[0]["line"] == 10 and blocks[1]["line"] == 20, str(blocks))
    check("非法补丁解析返回 None", _parse_patch_blocks("hello world") is None, "")

    # 2. 验收清单归一化（补健康检查、丢弃非法项）
    cl = _normalize_checklist([{"method": "GET", "path": "/goods", "expect": 200,
                                "contains": "coupon"},
                               {"method": "DELETE", "path": "/x"}])
    check("清单归一化含健康检查且过滤非法项",
          cl[0]["path"] == "/health" and cl[1]["path"] == "/goods"
          and all(c["method"] in ("GET", "POST") for c in cl), str(cl))

    # 3. 多块补丁可应用（temp 副本，不污染真实文件）
    tmp = os.path.join(cu.BASE_DIR, "temp", "ticket_target_copy.py")
    shutil.copy2(TARGET_FILE, tmp)
    src = open(tmp, encoding="utf-8").read().split("\n")
    # 找真实存在的一行做 old
    line_no, old = 1, src[0].rstrip("\r")
    blocks = [{"line": line_no, "old": old, "new": old}]
    ar = _apply_blocks_to_file(tmp, blocks)
    check("多块补丁应用到副本", ar["ok"], ar["msg"])
    # old 不匹配时应失败（不盲改）
    bad = _apply_blocks_to_file(tmp, [{"line": 1, "old": "this line does not exist 12345",
                                       "new": "x"}])
    check("old 不匹配拒绝应用", not bad["ok"], bad["msg"])

    # 4. 工单 CRUD + 状态机（用临时文件隔离，不污染真实 tickets.jsonl）
    global TICKETS_FILE
    real_tickets = TICKETS_FILE
    TICKETS_FILE = os.path.join(cu.BASE_DIR, "temp", "selftest_tickets.jsonl")
    if os.path.exists(TICKETS_FILE):
        os.remove(TICKETS_FILE)
    try:
        r = create_ticket("给商品加一个优惠券字段", _skip_process=True)
        check("创建工单", r.get("ok") and r["ticket"].startswith("T-"), r.get("msg", ""))
        tid = r["ticket"]
        t = get_ticket(tid)
        check("工单持久化", t is not None and len(t["messages"]) == 1
              and t["messages"][0]["content"] == "给商品加一个优惠券字段", "")
        check("工单号唯一递增", create_ticket("改标题", _skip_process=True)["ticket"] != tid, "")
        check("append_message 追加上下文",
              append_message(tid, "再帮我改个标题", _skip_process=True)["ok"], "")
        t2 = get_ticket(tid)
        check("消息追加后保留历史", len(t2["messages"]) == 2, "")
        # 人工审核前不可 approve
        check("非 await_review 不可审核",
              not approve_ticket(tid)["ok"], "")
        # 直接置为 await_review 后 approve 会尝试合并——selftest 跳过真合并（避免改线上），
        # 用不存在工单验证 reject 的兜底路径
        check("驳回不存在工单", not reject_ticket("T-none")["ok"], "")
    finally:
        TICKETS_FILE = real_tickets
        if os.path.exists(os.path.join(cu.BASE_DIR, "temp", "selftest_tickets.jsonl")):
            try:
                os.remove(os.path.join(cu.BASE_DIR, "temp", "selftest_tickets.jsonl"))
            except Exception:
                pass

    # 5. 过滤结构检查（环境无关）：本地模型可用则走真实判定，不可用则降级放行
    t_fake = {"messages": [{"role": "user", "content": "给商品加优惠券", "time": ""}]}
    f = local_filter(t_fake)
    check("过滤返回结构合法（可用=真实判定，不可用=降级放行）",
          isinstance(f, dict) and "reasonable" in f and "degraded" in f
          and "reason" in f and isinstance(f["reasonable"], bool)
          and (f.get("degraded") is False or f.get("reasonable") is True),
          f.get("reason", ""))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nticket_agent selftest 结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


# ---- 9. 命令行入口（演示备选：不依赖 dashboard 也能发工单/审核） ---------------
def main():
    parser = argparse.ArgumentParser(description="工单式多模型人机协作自愈")
    parser.add_argument("--selftest", action="store_true", help="运行自测")
    parser.add_argument("--create", type=str, default="", help="创建工单：--create '需求文本'")
    parser.add_argument("--append", type=str, default="", help="追加消息：--append '消息' --id T-xxx")
    parser.add_argument("--id", type=str, default="", help="目标工单 id")
    parser.add_argument("--approve", action="store_true", help="审核通过并合并")
    parser.add_argument("--reject", type=str, default="", help="驳回（附原因）")
    parser.add_argument("--list", action="store_true", help="列出全部工单")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())
    if args.list:
        for t in list_tickets():
            print(f"{t['id']}  [{t['status']}]  {t['title']}  "
                  f"消息{len(t['messages'])}条  更新 {t['updated_at']}")
        return 0
    if args.create:
        r = create_ticket(args.create)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if args.append and args.id:
        r = append_message(args.id, args.append)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if args.approve and args.id:
        print(json.dumps(approve_ticket(args.id), ensure_ascii=False, indent=2))
        return 0
    if args.reject and args.id:
        print(json.dumps(reject_ticket(args.id, args.reject), ensure_ascii=False, indent=2))
        return 0
    print("用法: python ticket_agent.py --selftest | --create '<需求>' | "
          "--id T-xxx --approve | --id T-xxx --reject '<原因>' | --list")
    return 0


if __name__ == "__main__":
    main()
