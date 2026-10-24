"""
self_heal.py —— 多模型分工 + 闭环自我修正（任务 H）

流程：
  1) 分析模型（deepseek-flash）判断故障类型 + 提取关键信息
  2) 代码模型（kimi-k2.7-code）生成补丁（统一 diff 格式）
  3) 沙箱验证（8081 端口 HTTP 请求断言）
  4) 失败 -> 把错误信息回传给代码模型重新生成
  5) 重试上限 MAX_RETRY=3，超过放弃并明确报错

硬约束：
  - MAX_RETRY = 3，超过返回 {"ok": false, "reason": "超过重试上限"}
  - 每次重试都往 events.jsonl 写一行（stage=patch，带 attempt 序号）
  - 任一模型调不通 -> 全部步骤降级用同一个模型 -> 再不行走离线规则库
  - 总 LLM 调用次数超 MAX_TOTAL_LLM_CALLS -> 直接转离线兜底

用法：
  python self_heal.py --selftest        # 内置自测（不依赖外网/沙箱可跑闭环）

为什么把"闭环"单独抽模块：main.py 的修 bug 链路是六 stage 事件编排的主流程，
闭环（生成补丁 -> 沙箱验证 -> 重试）是任务 H 新增的自我修正能力，独立成模块
可单独 --selftest，main 只负责把结果写进 events（保持六 stage 契约不变，
只新增 mode/attempt 字段）。
"""

import argparse
import json
import os
import sys

import common_utils as cu

# ---- 硬约束常量 ------------------------------------------------------------
MAX_RETRY = 3                # 闭环重试上限：补丁生成+沙箱验证最多尝试 3 轮
MAX_TOTAL_LLM_CALLS = 8      # 单次自愈流程的 LLM 总调用次数上限（防现场无限烧钱）

# 多模型分工的别名（config.json providers 里的 name）
ANALYZE_MODEL = "deepseek-v4.1-flash"     # 分析模型：故障类型 + 关键信息
CODE_MODEL = "kimi-k2.7-code"                  # 代码模型：生成补丁（正确模型名）

# 本地模型（Ollama 兼容端点；与 diagnose.py 的 LOCAL_* 保持一致）
LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_MODEL = "qwen3:8b"
LOCAL_API_KEY = "local"


def _patch_prompt(parsed, context, tb_text, feedback=None):
    """代码模型生成补丁的 prompt。
    context 是出错行附近的源码行列表；feedback 是上一次沙箱验证的错误信息
    （重试时回传，让模型"看到自己错在哪"再改）。
    自动注入目标文件的 import 区域，避免模型引用未导入的符号（如 abort）。"""
    ctx = "\n".join(context) if context else "(无源码上下文)"
    # 注入目标文件 import 区域，约束可用符号
    imports_hint = "(无法读取目标文件 import 区域)"
    try:
        target_path = os.path.join(cu.BASE_DIR, "target_app.py")
        with open(target_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        imp_lines = [ln.rstrip() for ln in lines[:60]
                     if ln.strip().startswith(("import ", "from ", "# -*-"))]
        if imp_lines:
            imports_hint = "\n".join(imp_lines[:25])
    except Exception:
        pass
    fb = ""
    if feedback:
        fb = ("\n注意：上一版补丁在沙箱验证失败，错误信息如下，请修正补丁：\n"
              "```\n%s\n```" % feedback)
    return [
        {"role": "system", "content": "你是资深 Python 后端工程师，只输出统一 diff 补丁，"
                                      "不要任何解释。补丁格式必须是三行：\n"
                                      "line: <行号>\nold: <原行完整内容>\nnew: <替换后的完整内容>"},
        {"role": "user", "content":
            "故障 traceback：\n%s\n出错点：%s:%s\n出错行附近源码：\n%s\n"
            "目标文件顶部 import 区域（只能使用其中已导入的符号，禁止引用未导入名字）：\n%s%s"
            % (tb_text, parsed["file"], parsed["line"], ctx, imports_hint, fb)},
    ]


def resolve_models():
    """解析多模型分工的两个 provider：
    分析模型优先 deepseek-v4.1-flash；代码模型优先 kimi-k2.7-code。
    "任一模型调不通 -> 全部步骤降级用同一个模型"：kimi 没配 key / 不可用时，
    代码模型自动复用"分析模型"（或任意可用模型）。
    返回 {"analyze": provider|None, "code": provider|None}。"""
    analyze = cu.resolve_provider(ANALYZE_MODEL)
    if not (analyze and analyze.get("api_key")):
        analyze = cu.any_available_provider()

    code = cu.resolve_provider(CODE_MODEL)
    if not (code and code.get("api_key")):
        code = analyze   # 降级：同一模型承担分析+补丁

    return {"analyze": analyze, "code": code}


def analyze(tb_text, parsed=None, context=None, provider=None):
    """① 分析模型：判断故障类型 + 提取关键信息。
    返回 {"fault_type","key_info","raw"} 或 None（模型不可用/输出非法）。"""
    parsed = parsed or cu.parse_traceback(tb_text)
    # 不自动 resolve：调用方（run_repair）已显式传入解析好的 provider；
    # provider=None 表示该模型不可用，直接返回 None 不抛异常。
    if not provider:
        return None
    ctx = "\n".join(context) if context else "(无源码上下文)"
    messages = [
        {"role": "system", "content": "你是故障分析专家，只输出 JSON。"},
        {"role": "user", "content": "traceback：\n%s\n出错点 %s:%s，附近源码：\n%s\n"
                                    '请输出 {"fault_type":"code_error|traffic_surge|'
                                    'dependency_fail|unknown", "key_info":"提取的关键信息"}' %
                                    (tb_text, parsed["file"], parsed["line"], ctx)},
    ]
    content = cu.llm_chat(messages, max_tokens=1500, provider=provider, timeout=30)
    if not content:
        return None
    import re
    m = re.search(r"\{.*\}", content, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except Exception:
        data = None
    if not isinstance(data, dict):
        return None
    return {"fault_type": data.get("fault_type", "unknown"),
            "key_info": data.get("key_info", ""), "raw": content[:100]}


def generate_patch(parsed, context, tb_text, provider=None, feedback=None):
    """② 代码模型：生成统一 diff 格式补丁（可带重试反馈）。
    返回补丁文本或 None（模型不可用/输出非法）。"""
    # 不自动 resolve：调用方（run_repair）已显式传入解析好的 provider；
    # provider=None 表示该模型不可用，直接返回 None 不抛异常。
    if not provider:
        return None
    content = cu.llm_chat(_patch_prompt(parsed, context, tb_text, feedback=feedback),
                          max_tokens=4000, provider=provider, timeout=120)
    if not content:
        return None
    # 校验格式：必须含 line:/old:/new:
    import re
    if not (re.search(r"line:\s*\d+", content)
            and re.search(r"old:", content) and re.search(r"new:", content)):
        return None
    return content.strip()


def _local_patch(parsed, context, tb_text, feedback=None):
    """③ 本地模型（Ollama）生成补丁；失败返回 None（降级离线）。"""
    local_provider = {"name": "local-ollama", "model": LOCAL_MODEL,
                      "base_url": LOCAL_BASE_URL, "api_key": LOCAL_API_KEY}
    content = cu.llm_chat(_patch_prompt(parsed, context, tb_text, feedback=feedback),
                          max_tokens=600, provider=local_provider, timeout=8)
    if not content:
        return None
    import re
    if not (re.search(r"line:\s*\d+", content)
            and re.search(r"old:", content) and re.search(r"new:", content)):
        return None
    return content.strip()


def run_repair(tb_text, parsed=None, context=None, force_local=False,
               write_event=None, check_paths=("/health",), target_file=None,
               max_retry=MAX_RETRY):
    """③④⑤ 闭环自我修正：分析 -> 生成补丁 -> 沙箱验证 -> 失败重试（≤MAX_RETRY）。
    返回 {"ok","patch","verify","attempts","mode","reason","call_count",
          "analysis"}。
    - write_event 是可选回调 write_event(stage, **fields)：任务 H 要求"每次
      重试都往 events.jsonl 写一行（stage=patch，带 attempt 序号）"，main 会
      传真实 cu.write_event 包装；selftest 传打印函数验证行为。
    - 任一模型调不通：代码模型降级用分析模型（同一模型）-> 本地 -> 离线。
    - LLM 总调用次数超 MAX_TOTAL_LLM_CALLS：直接转离线兜底。
    - 超过重试上限：返回 {"ok": false, "reason": "超过重试上限"}。"""
    parsed = parsed or cu.parse_traceback(tb_text)
    context = context or []
    if not target_file:
        target_file = os.path.join(cu.BASE_DIR, "target_app.py")

    call_count = 0
    analysis = None

    def ev(stage, **fields):
        if write_event:
            try:
                write_event(stage, **fields)
            except Exception:
                pass

    # ---- ① 分析模型（失败不阻塞闭环，patch 流程不依赖它）----
    models = resolve_models()
    if force_local:
        models = {"analyze": None, "code": None}
    if models["analyze"]:
        call_count += 1
        analysis = analyze(tb_text, parsed=parsed, context=context,
                           provider=models["analyze"])

    # ---- ② 生成补丁（代码模型 -> 降级分析模型 -> 本地 -> 离线）----
    # 为什么代码模型失败要降级分析模型：实测 kimi(代码) 对 NotFound/WebsocketMismatch
    # 等异常返回空 content，而 deepseek(分析) 能稳定生成合法补丁；直接跳离线会白白
    # 损失一次云端修复机会（任务 M 补丁生成少的主要根因）。
    def _gen(feedback=None):
        nonlocal call_count
        if not force_local:
            # ① 代码模型（如 kimi）优先
            if models["code"]:
                call_count += 1
                if call_count > MAX_TOTAL_LLM_CALLS:
                    return None, "limit"
                patch = generate_patch(parsed, context, tb_text,
                                       provider=models["code"], feedback=feedback)
                if patch:
                    return patch, "cloud"
            # ② 代码模型不可用/返回空：降级用分析模型再生成一次（同一链路，能修大多数错误）
            if models["analyze"]:
                call_count += 1
                if call_count > MAX_TOTAL_LLM_CALLS:
                    return None, "limit"
                patch = generate_patch(parsed, context, tb_text,
                                       provider=models["analyze"], feedback=feedback)
                if patch:
                    return patch, "cloud"
            return None, "cloud"
        # 本地模型
        call_count += 1
        if call_count > MAX_TOTAL_LLM_CALLS:
            return None, "limit"
        return _local_patch(parsed, context, tb_text, feedback=feedback), "local"

    patch, mode = _gen()
    if not patch and mode == "limit":
        patch = None
        patch, mode = _offline_patch(tb_text, parsed), "offline"
    if not patch:
        # 代码模型走完还没补丁：本地走不通就离线规则库兜底
        patch, mode = _offline_patch(tb_text, parsed), "offline"

    if not patch:
        return {"ok": False, "patch": "", "verify": None, "attempts": 0,
                "mode": mode, "reason": "补丁生成失败（云端/本地/离线均不可用）",
                "call_count": call_count, "analysis": analysis}

    # ---- ③④⑤ 沙箱验证 + 失败重试（≤MAX_RETRY）----
    attempts = 0
    feedback = ""
    last_verify = None
    while attempts < max_retry:
        attempts += 1
        ev("patch", suggestion=patch, file=os.path.basename(target_file),
           line=parsed.get("line"), mode=mode, attempt=attempts)
        verify = cu.sandbox_verify(target_file, patch, check_paths=check_paths)
        last_verify = verify
        if verify["ok"]:
            return {"ok": True, "patch": patch, "verify": verify,
                    "attempts": attempts, "mode": mode, "reason": "",
                    "call_count": call_count, "analysis": analysis}
        # 失败：回传错误信息让代码模型重生成
        feedback = verify.get("error") or "沙箱验证未通过"
        if attempts < max_retry:
            ev("patch", retry=True, attempt=attempts + 1, mode=mode,
               feedback=feedback[:120])
        patch, mode = _gen(feedback=feedback)
        if not patch:
            patch, mode = _offline_patch(tb_text, parsed), "offline"

    return {"ok": False, "patch": "", "verify": last_verify,
            "attempts": attempts, "mode": mode, "reason": "超过重试上限",
            "call_count": call_count, "analysis": analysis}


def _offline_patch(tb_text, parsed):
    """离线规则库兜底：直接复用 offline_fallback.build_patch（已有逻辑不变）。
    为什么只传 tb_text：offline_fallback.build_patch 的接口只接收 traceback
    文本，内部自己会 parse；传 parsed 进去反而 TypeError。"""
    try:
        import offline_fallback as of
        return of.build_patch(tb_text)
    except Exception:
        return None


# ---- 自测（--selftest）-----------------------------------------------------
def run_selftest():
    """自测要点：① 闭环在离线兜底下可产出补丁（不依赖外网）；
    ② MAX_RETRY 超限返回 reason="超过重试上限"；③ 每次重试写 events(patch,
    attempt)；④ 模型降级复用逻辑（kimi 无 key -> 复用 deepseek）。"""
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    import offline_fallback as of
    tb = ("Traceback (most recent call last):\n"
          '  File "target_app.py", line 326, in get_goods\n'
          '    discount = goods["price"] / goods["stock"]    # \u5e93\u5b58 0 \u65f6\u5d29\n'
          "ZeroDivisionError: division by zero")
    parsed = cu.parse_traceback(tb)

    # 1. 离线规则库兜底能产出补丁
    patch = _offline_patch(tb, parsed)
    check("离线兜底产出统一 diff 补丁", bool(patch) and "line:" in patch,
          (patch or "")[:60])

    # 2. 分析模型不可用（无 provider）时返回 None 不抛异常
    a = analyze(tb, parsed=parsed, provider=None)
    check("分析模型不可用返回 None", a is None, str(a))

    # 3. 代码模型不可用（无 provider）时生成补丁返回 None
    p = generate_patch(parsed, [], tb, provider=None)
    check("代码模型不可用返回 None", p is None, str(p))

    # 4. 模型降级复用：kimi 没 key 时 code 复用 analyze（deepseek 有 key）
    models = resolve_models()
    check("多模型降级复用（code 复用可用模型）",
          models["code"] is not None and models["code"].get("name") in
          ("kimi-k2.7-code", "deepseek-v4.1-flash", models["analyze"].get("name") if models["analyze"] else ""),
          str({k: (v.get("name") if v else None) for k, v in models.items()}))

    # 5. 闭环（force_local + 本地不可用 -> 离线模式）能产出补丁并通过沙箱验证
    #    这里直接跑 run_repair：真实起 8081 沙箱验证（target_app 默认 4 bug 关闭时 /health 200）
    events_written = []

    def fake_write(stage, **fields):
        events_written.append((stage, fields))

    r = run_repair(tb, parsed=parsed,
                   context=['        discount = goods["price"] / goods["stock"]'],
                   force_local=True, write_event=fake_write)
    check("闭环离线兜底完成自愈", r.get("ok") is True and r.get("mode") == "offline",
          str({k: r.get(k) for k in ("ok", "mode", "attempts", "reason")}))
    check("闭环写 events(patch, attempt)", any(
        s == "patch" and f.get("attempt") for s, f in events_written),
        str(events_written[:2])[:100])

    # 6. 超过重试上限返回 {"ok": false, "reason": "超过重试上限"}
    #    构造方式：让沙箱验证永远失败（check_paths 指向一个 404 路径）
    r2 = run_repair(tb, parsed=parsed,
                    context=['        discount = goods["price"] / goods["stock"]'],
                    force_local=True, write_event=fake_write,
                    check_paths=("/no-such-path-404",),
                    max_retry=3)
    check("超过重试上限报错", r2.get("ok") is False and r2.get("reason") == "超过重试上限"
          and r2.get("attempts") == 3,
          f"ok={r2.get('ok')} reason={r2.get('reason')} attempts={r2.get('attempts')}")

    # 7. MAX_RETRY 常量正确
    check("MAX_RETRY=3", MAX_RETRY == 3, str(MAX_RETRY))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nself_heal selftest 结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


def main():
    parser = argparse.ArgumentParser(description="多模型分工 + 闭环自我修正")
    parser.add_argument("--selftest", action="store_true", help="运行自测")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(run_selftest())
    print("用法: python self_heal.py --selftest")


if __name__ == "__main__":
    main()
