"""
common_utils.py —— 公共工具模块（main.py / feature_agent.py / dashboard.py 共用）

职责：
  1. events.jsonl 写入（六 stage 数据契约，绝对不能改格式）
  2. traceback 解析（文件/行号/异常类型）
  3. 日志增量读取（watcher 专用，保证不重复触发）
  4. 沙箱验证（复制代码到临时目录 -> 起 8081 进程 -> 验证 -> 强杀，无残留）

为什么抽公共模块：main 和 feature_agent 都要用沙箱、events、LLM 兜底，
复制三份容易改出不一致，统一收在这里，改一处全生效。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

# ---- 路径 ---------------------------------------------------------------
# 所有数据文件固定在代码所在目录，演示时整目录拷贝即可。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(BASE_DIR, "events.jsonl")
APP_LOG = os.path.join(BASE_DIR, "app.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SANDBOX_QUEUE_FILE = os.path.join(BASE_DIR, "sandbox_queue.json")
# AI 交流日志（独立于 events.jsonl 六 stage 契约）：记录每次与 LLM 的
# 请求/响应，供控制台"AI 交流"面板回放。只读侧新增文件，不影响契约。
AI_CHAT_FILE = os.path.join(BASE_DIR, "ai_chat.jsonl")

STAGES = ("alert", "locate", "reason", "patch", "apply", "verify")
DEFAULT_MODEL = "deepseek-v4.1-flash"   # 默认模型别名（config.json 里 providers 的 name）


# ---- 1. events.jsonl 数据契约 --------------------------------------------
def write_event(stage, **fields):
    """往 events.jsonl 追加一行 JSON。stage 必须是六种之一，time 自动打当前时间。
    为什么用追加模式：Agent 每完成一步写一行，控制台按时间顺序读出来就是"时间轴"，
    这是整场比赛唯一的数据契约，字段名不可改。"""
    if stage not in STAGES:
        stage = "reason"  # 不认识的 stage 兜底为 reason，绝不写坏契约
    # 为什么 time 之外再加 date：数据契约只约束 stage 六种取值和 time 字段，
    # 追加 date 不影响契约；驾驶舱"按日期筛选日志"需要知道每行是哪天的。
    event = {"time": time.strftime("%H:%M:%S"),
             "date": time.strftime("%Y-%m-%d"), "stage": stage}
    event.update(fields)
    line = json.dumps(event, ensure_ascii=False)
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[common] 写 events 失败: {e}")  # 日志失败不能让 Agent 崩
    return line


# ---- 2. traceback 解析 ----------------------------------------------------
def parse_traceback(tb_text):
    """提取 traceback 里的 (文件, 行号, 异常类型)。取最后一个 File 行 = 最内层出错点。"""
    tb = tb_text or ""
    files = re.findall(r'File "([^"]+)", line (\d+)', tb)
    last = files[-1] if files else ("unknown", 0)
    lines = [l for l in tb.splitlines() if l.strip()]
    m = re.search(r"([A-Za-z]+Error)(?::|\s*$)", lines[-1] if lines else "")
    error_type = m.group(1) if m else "UnknownError"
    return {"file": last[0], "line": int(last[1]), "error": error_type}


# ---- 3. 日志增量读取（watcher 专用） ---------------------------------------
def read_log_tail(path, offset):
    """从文件 offset 字节位置往后读，返回 (新增文本, 新 offset)。
    为什么必须记录 offset 而不是每次读全量：watcher 每秒读一次，如果每次都从头读，
    同一个故障会被重复触发无数次（比赛最容易翻车的点）。"""
    if not os.path.exists(path):
        return "", offset
    size = os.path.getsize(path)
    if size < offset:  # 文件被清空/重建了，重置到 0
        offset = 0
    if size == offset:  # 没有新内容
        return "", offset
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        new_text = f.read()
    return new_text, size


# ---- 4. 沙箱验证 ------------------------------------------------------------
def make_sandbox_dir(prefix="sandbox"):
    """建一个独立沙箱目录（中间产物，放 temp），返回目录路径。"""
    temp_dir = os.path.join(BASE_DIR, "temp", f"{prefix}_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def apply_patch_to_file(target_path, patch_text):
    """把"统一 diff 格式"的补丁应用到文件。
    补丁格式（main / feature_agent 里生成，大模型也按这个格式输出）：
        line: <行号>
        old: <当前行的原始内容>
        new: <替换成的内容>
    为什么同时校验 line 和 old：只靠行号怕补丁写错行，先确认目标行内容和
    old 完全一致才替换；对不上就返回错误，绝不把文件改坏。"""
    patch = patch_text or ""
    m_line = re.search(r"line:\s*(\d+)", patch)
    m_old = re.search(r"old:\s*(.*)", patch)
    m_new = re.search(r"new:\s*(.*)", patch)
    if not (m_line and m_old and m_new):
        return {"ok": False, "msg": "补丁格式错误，缺少 line/old/new"}

    line_no = int(m_line.group(1))
    old_text = m_old.group(1).rstrip("\r")
    new_text = m_new.group(1).rstrip("\r")

    with open(target_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if line_no < 1 or line_no > len(lines):
        return {"ok": False, "msg": f"行号 {line_no} 超出文件范围 ({len(lines)} 行)"}
    current = lines[line_no - 1].rstrip("\r\n")
    if current.strip() != old_text.strip():
        return {"ok": False, "msg": f"第 {line_no} 行内容不匹配:\n当前: {current}\n期望: {old_text}"}

    # 为什么补缩进：出错行往往在 if/for 块内（如"if BUG_FLAGS[1]: order = ..."），
    # 大模型或规则库生成 new 时可能不带前导空格，直接替换会造成 IndentationError，
    # 服务一启动就崩。这里自动继承原行缩进，从根上杜绝这类翻车。
    indent = current[:len(current) - len(current.lstrip())]
    new_text = indent + new_text.lstrip()
    lines[line_no - 1] = new_text + "\n"
    with open(target_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return {"ok": True, "msg": f"已替换第 {line_no} 行", "line": line_no, "old": old_text, "new": new_text}


def _sandbox_verify_impl(target_file, patch_text, port=8081, check_paths=("/health",),
                         timeout=30, workdir=None):
    """沙箱验证核心逻辑（排队版由 sandbox_verify 包一层，同一时刻只跑一个请求）。
    把修复后的代码复制到临时目录，起 port 独立进程，逐个请求 check_paths，
    全部 200 才算通过；最后强制 kill 进程，保证无残留。

    返回 {"ok": bool, "results": [{path, status}], "error": str}"""
    results = []
    proc = None
    try:
        sandbox_dir = workdir or make_sandbox_dir()
        # 复制 target_app.py 到沙箱（为什么复制而不是直接改：线上文件不能拿来
        # 试错，沙箱里随便崩都不影响正在运行的 8080 服务）
        sandbox_target = os.path.join(sandbox_dir, os.path.basename(target_file))
        shutil.copy2(target_file, sandbox_target)

        # 应用补丁到沙箱副本
        patch_res = apply_patch_to_file(sandbox_target, patch_text)
        if not patch_res["ok"]:
            return {"ok": False, "results": [], "error": f"沙箱内应用补丁失败: {patch_res['msg']}"}

        # 起沙箱服务（独立端口，与线上隔离）
        proc = subprocess.Popen(
            [sys.executable, sandbox_target, "--port", str(port), "--no-threaded"],
            cwd=sandbox_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # 轮询等待服务起来（最多 timeout 秒）
        deadline = time.time() + timeout
        alive = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                    if resp.status == 200:
                        alive = True
                        break
            except Exception:
                time.sleep(0.5)
        if not alive:
            return {"ok": False, "results": [], "error": f"沙箱服务 {port} 端口 {timeout}s 内未启动"}

        # 逐条验证
        for path in check_paths:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                    results.append({"path": path, "status": resp.status})
            except Exception as e:
                results.append({"path": path, "status": 0, "error": str(e)})

        all_ok = all(r["status"] == 200 for r in results)
        return {"ok": all_ok, "results": results, "error": "" if all_ok else "存在非 200 的验证请求"}
    finally:
        # 为什么 finally 里强杀：比赛最怕沙箱进程残留占着 8081，
        # 下次验证就起不来了。terminate 不行就 taskkill（Windows 专用），再不行超时自杀。
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


def sandbox_verify(target_file, patch_text, port=8081, check_paths=("/health",),
                   timeout=30, workdir=None):
    """沙箱验证（排队版）：任何进程要跑沙箱必须先进入统一队列，
    同一时刻只有一个请求在沙箱里执行，其余在排队区等待，轮到后自动执行。
    为什么排队：修 bug 链路（main）和自动开发链路（feature_agent）共用 8081，
    两个请求同时起进程会互相踩端口；驾驶舱也要能直观看到"谁在用沙箱、几个在等"。"""
    request_id = f"repair-{int(time.time() * 1000)}"
    return with_sandbox_queue(request_id, "repair", lambda: _sandbox_verify_impl(
        target_file, patch_text, port=port, check_paths=check_paths,
        timeout=timeout, workdir=workdir))


# ---- 4.1 沙箱排队机制 -------------------------------------------------------
# sandbox_queue.json 结构（多进程共用同一文件，读写用临时文件+os.replace 原子替换）：
#   {"current": {"request_id","type","time","status"} | null,
#    "queue":   [{"request_id","type","time","status":"waiting"}, ...]}

def _read_queue():
    """读沙箱队列状态（文件缺失/损坏时返回空结构，任何情况不抛异常）。"""
    default = {"current": None, "queue": []}
    if not os.path.exists(SANDBOX_QUEUE_FILE):
        return default
    try:
        with open(SANDBOX_QUEUE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        return {"current": data.get("current"), "queue": data.get("queue") or []}
    except Exception:
        return default


def _write_queue(data):
    """原子写队列文件：先写 .tmp 再 os.replace 覆盖，避免多进程读到半截 JSON。"""
    try:
        os.makedirs(os.path.dirname(SANDBOX_QUEUE_FILE), exist_ok=True)
        tmp = SANDBOX_QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SANDBOX_QUEUE_FILE)
        return True
    except Exception:
        return False


def get_sandbox_status():
    """沙箱状态（驾驶舱"沙箱状态区"展示用）：当前占用 + 排队列表。"""
    q = _read_queue()
    return {"current": q["current"], "queue": q["queue"]}


def enqueue_request(request_id, req_type="repair"):
    """加入沙箱排队区（幂等：同 request_id 不重复入队）。
    返回排队位置（0 = 队首，前面还有 n-1 个）。"""
    q = _read_queue()
    q["queue"] = [i for i in q["queue"] if i.get("request_id") != request_id]
    q["queue"].append({"request_id": request_id, "type": req_type,
                       "time": time.strftime("%H:%M:%S"), "status": "waiting"})
    _write_queue(q)
    return len(q["queue"]) - 1


def acquire_sandbox(request_id, req_type="repair"):
    """尝试占用沙箱：仅当沙箱空闲且自己排在队首时才成功（严格 FIFO）。
    成功返回 True，否则返回 False（继续等待）。"""
    q = _read_queue()
    if q["current"] is not None:
        return False
    # 队列里有人排在自己前面就绝不抢先（保证"先来先到"，驾驶舱看到的排队顺序
    # 就是实际执行顺序）
    if q["queue"] and q["queue"][0].get("request_id") != request_id:
        return False
    q["current"] = {"request_id": request_id, "type": req_type,
                    "time": time.strftime("%H:%M:%S"), "status": "running"}
    # 从队列移除自己（已转正为 current）
    q["queue"] = [i for i in q["queue"] if i.get("request_id") != request_id]
    return _write_queue(q)


def release_sandbox(request_id=None):
    """释放沙箱占用。request_id 为空或匹配时才释放（防误释放别人的占用）。"""
    q = _read_queue()
    if q["current"] is None:
        return
    if request_id is not None and q["current"].get("request_id") != request_id:
        return
    q["current"] = None
    _write_queue(q)


def with_sandbox_queue(request_id, req_type, worker, timeout=600):
    """排队执行沙箱任务：入队 -> 轮询等待轮到（当前空闲且队首）-> 执行 worker ->
    释放。任何沙箱任务都走这个入口，保证全局只有一个在跑。返回 worker() 结果。"""
    enqueue_request(request_id, req_type)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if acquire_sandbox(request_id, req_type):
                break
            time.sleep(0.5)
        else:
            return {"ok": False, "results": [], "error": "沙箱排队超时，未能获得沙箱"}
        return worker()
    finally:
        release_sandbox(request_id)


# ---- 5. 大模型调用（main / feature_agent 共用，失败自动返回 None） ------------
# 配置来源优先级：环境变量（OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME）
# -> config.json（openai_base_url / openai_api_key / model_name / providers）
# 多模型扩展设计：providers 是模型池，如
#   [{"name": "deepseek-v4.1-flash", "model": "DeepSeek-V4.1-Flash",
#     "base_url": "https://api.modelverse.cn/v1", "api_key": "...", "enabled": true},
#    {"name": "kimi-k2.7-code", "model": "kimi-k2.7-code", ...}]
# model_name 是当前选中的 provider 别名；真实模型 ID 从 providers 里映射。
# 以后要接第二个模型，只需在 config.json 的 providers 里加一项，并在
# 设置面板切换 model_name，代码链路（llm_chat / Agent）完全不用改。

def load_llm_config():
    """读取 LLM 配置（环境变量优先，fallback config.json）。返回
    {"base_url","api_key","model_name","providers"}。任何读取失败都不抛异常。"""
    cfg = {
        "base_url": os.environ.get("OPENAI_BASE_URL", ""),
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "model_name": os.environ.get("MODEL_NAME", ""),
    }
    providers = []
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            if not cfg["base_url"]:
                cfg["base_url"] = saved.get("openai_base_url", "")
            if not cfg["api_key"]:
                cfg["api_key"] = saved.get("openai_api_key", "")
            if not cfg["model_name"]:
                cfg["model_name"] = saved.get("model_name", "")
            providers = saved.get("providers", []) or []
        except Exception:
            pass
    if not cfg["model_name"]:
        cfg["model_name"] = DEFAULT_MODEL
    return {**cfg, "providers": providers}


def resolve_llm():
    """解析最终要用的 {base_url, api_key, model, model_name}。
    model_name 是别名，真实 model ID 从 providers 中匹配；匹配不到就直接当 model 用。"""
    cfg = load_llm_config()
    base_url, api_key, model_name = cfg["base_url"], cfg["api_key"], cfg["model_name"]
    model = model_name
    for p in cfg["providers"]:
        if p.get("name") == model_name and p.get("enabled", True):
            model = p.get("model") or model_name
            if not base_url:
                base_url = p.get("base_url", "")
            if not api_key:
                api_key = p.get("api_key", "")
            break
    return {"base_url": base_url or None, "api_key": api_key,
            "model": model, "model_name": model_name}


def resolve_provider(name):
    """按别名从 config.json 的 providers 里找指定模型（多模型分工用）。
    返回 {"name","model","base_url","api_key"} 或 None（不存在/未启用/无 key）。
    为什么需要按名查找：任务 H 的多模型分工里"分析模型=deepseek、
    代码模型=kimi"是两件不同的事，不能都走"当前选中模型"这一个口子。"""
    cfg = load_llm_config()
    for p in cfg["providers"]:
        if p.get("name") == name and p.get("enabled", True):
            return {
                "name": p.get("name"),
                "model": p.get("model") or p.get("name"),
                "base_url": p.get("base_url", ""),
                "api_key": p.get("api_key", ""),
            }
    return None


def any_available_provider():
    """返回 providers 里第一个"有 key 且启用"的模型（多模型降级复用用），
    没有则 None。为什么要有它：任务 H 要求"任一模型调不通→全部步骤降级
    用同一个模型"，kimi 没配 key 时就用它顶上 deepseek。"""
    cfg = load_llm_config()
    for p in cfg["providers"]:
        if p.get("enabled", True) and p.get("api_key"):
            return {
                "name": p.get("name"),
                "model": p.get("model") or p.get("name"),
                "base_url": p.get("base_url", ""),
                "api_key": p.get("api_key", ""),
            }
    return None


def write_ai_chat(model, messages, response, ok=True, stage="", duration=None, note=""):
    """记录一次与 LLM 的交流（请求/响应），写入 ai_chat.jsonl。
    独立于 events.jsonl 契约，供控制台"AI 交流"面板回放演示。
    messages 可含多轮，取最后一条 user 内容作为"请求"展示；
    response 为模型返回正文（失败时可为空串或错误描述）。"""
    try:
        # 取请求正文：最后一条 role=user 的消息，没有则拼接全部
        req = ""
        if messages:
            last_user = [m for m in messages if m.get("role") == "user"]
            req = last_user[-1].get("content", "") if last_user else ""
            if not isinstance(req, str):
                try:
                    req = json.dumps(req, ensure_ascii=False)
                except Exception:
                    req = str(req)
            if not req:
                req = json.dumps(messages[-1], ensure_ascii=False)
        if len(req) > 2000:
            req = req[:2000] + "…[截断]"
        resp = (response or "") if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        if len(resp) > 2000:
            resp = resp[:2000] + "…[截断]"
        rec = {
            "time": time.strftime("%H:%M:%S"),
            "date": time.strftime("%Y-%m-%d"),
            "model": model or "",
            "stage": stage or "",
            "ok": bool(ok),
            "duration": duration,
            "request": req,
            "response": resp,
            "note": note or "",
        }
        with open(AI_CHAT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[common] 写 ai_chat 失败: {e}")  # 交流日志失败不能影响主流程


def load_ai_chats():
    """读取 ai_chat.jsonl 全部记录（损坏行跳过，不抛异常）。"""
    recs = []
    try:
        with open(AI_CHAT_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return recs


def llm_chat(messages, max_tokens=600, provider=None, timeout=20):
    """调用 OpenAI 兼容接口（模型/地址/Key 从配置读取，不再硬编码）。
    任何失败（没 key / 断网 / 超时）都返回 None，让上层退本地规则库。
    为什么把失败吞掉：断网是现场常态，调用方只需要知道"模型没给结果"，
    不需要看到堆栈。

    provider 参数（任务 H 多模型分工用）：
      传入 dict {"name","model","base_url","api_key"} 时使用指定模型；
      不传则用"当前选中模型"（行为与旧版完全一致，向后兼容）。
    timeout 可缩短（如诊断降级链路上云端只等 8 秒，现场断网不用干等）。"""
    import time as _time
    _start = _time.time()
    if provider:
        if not provider.get("api_key"):
            return None
        base_url = provider.get("base_url") or None
        api_key = provider["api_key"]
        model = provider.get("model") or provider.get("name")
    else:
        llm = resolve_llm()
        if not llm["api_key"]:
            return None
        base_url, api_key, model = llm["base_url"], llm["api_key"], llm["model"]
    try:
        from openai import OpenAI
        # 推理模型（如 deepseek-v4.1-flash 默认 high effort）会先烧思维链 token，
        # max_tokens 太小会导致 content 为空，保底抬到 400。
        max_tokens = max(int(max_tokens), 400)
        client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout)
        resp = client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens)
        content = resp.choices[0].message.content
        ok = content is not None and len(content.strip()) > 0
        duration = round(_time.time() - _start, 2)
        note = f"{resp.usage.total_tokens} tokens (prompt={resp.usage.prompt_tokens}, completion={resp.usage.completion_tokens})" if resp.usage else ""
        write_ai_chat(model=model, messages=messages, response=content or "", ok=ok, stage="chat", duration=duration, note=note)
        return content if content else None
    except Exception:
        duration = round(_time.time() - _start, 2)
        write_ai_chat(model=model, messages=messages, response="", ok=False, stage="chat", duration=duration, note="exception")
        return None


def http_get(url, timeout=3):
    """标准库 GET 请求，任何失败返回 None。为什么不用 requests：
    比赛只允许 flask/streamlit/openai/streamlit-autorefresh + 标准库。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def service_alive(port=8080, timeout=2):
    """检查某端口服务是否活着（Agent 判断服务状态用）。"""
    r = http_get(f"http://127.0.0.1:{port}/health", timeout=timeout)
    return r is not None and r[0] == 200


# ---- 6. 事件日志清空（驾驶舱"清空"按钮用，先备份再清，不物理删除） ----------
def backup_and_clear_events():
    """清空 events.jsonl：先整份备份到 temp（带时间戳），再清空原文件。
    为什么必须备份：事件日志是演示回放的素材，物理删除不可逆；备份到 temp
    后即使误点清空也能翻旧账。返回 {"ok", "backup", "msg"}。"""
    if not os.path.exists(EVENTS_FILE):
        return {"ok": True, "backup": None, "msg": "events.jsonl 不存在，无需清空"}
    try:
        temp_dir = os.path.join(BASE_DIR, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        backup = os.path.join(
            temp_dir, f"events_backup_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
        shutil.copy2(EVENTS_FILE, backup)
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            f.write("")
        return {"ok": True, "backup": backup,
                "msg": f"已备份到 {backup} 并清空（备份保留在 temp 目录，可随时找回）"}
    except Exception as e:
        return {"ok": False, "backup": None, "msg": f"清空失败: {e}"}


def load_events():
    """读取 events.jsonl 全部事件（缺失/损坏行跳过，绝不抛异常）。"""
    events = []
    if not os.path.exists(EVENTS_FILE):
        return events
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return events
    return events
