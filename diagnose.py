"""
diagnose.py —— 统一诊断接口（任务 H：三级降级）

diagnose(log) -> dict，内部按序尝试、失败自动降级：
  ① 云端大模型（分析模型：deepseek-v4.1-flash，kimi 可复用）
  ② 本地模型（Ollama / OpenAI 兼容 http://localhost:11434/v1，model=qwen3:8b）
  ③ 离线规则库（offline_fallback，已有逻辑保持不变，零依赖保底）

返回 {"location", "cause", "fix", "mode", "error_type", "code_line"}，
mode 取值 "cloud" / "local" / "offline"。
mode 必须写入 events.jsonl（六 stage 契约不变，只新增 mode 字段）并在
控制台（dashboard.py）显示，让评委看到降级设计。
上层调用方完全无感知、永远只调 diagnose()。

用法：
  python diagnose.py                     # 用内置样例跑一遍三级降级演示
  python diagnose.py --local             # 强制走本地模型（方便测试降级链路）
  python diagnose.py --selftest          # 内置自测（不依赖外网/本地模型）
"""

import argparse
import json
import os
import re
import sys

import common_utils as cu

# 云端诊断用的模型别名（config.json providers 里的 name）。
# 分析模型负责"判断故障类型 + 提取关键信息 + 定位原因与修复建议"。
CLOUD_ANALYZE_ALIASES = ("deepseek-v4.1-flash", "kimi-k2.7-code")

# 本地模型（Ollama 的 OpenAI 兼容端点；没装 Ollama 时连接会立刻失败 -> 秒级降级）
LOCAL_BASE_URL = "http://localhost:11434/v1"
LOCAL_MODEL = "qwen3:8b"
LOCAL_API_KEY = "local"          # Ollama 不校验 key，占位即可

# 云端诊断超时（秒）：现场网络极差，云端等太久会拖慢整条降级链。
# 断网时 8 秒超时 -> 本地连接失败 -> 离线规则库，总耗时约 10 秒以内可接受。
CLOUD_TIMEOUT = 8


def _parse_llm_json(text):
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 围栏、前后废话）。"""
    text = text or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _diag_prompt(tb_text):
    return [
        {"role": "system", "content": "你是服务端故障诊断专家。"
                                      "只输出 JSON，不要解释、不要代码块围栏。"},
        {"role": "user", "content": "下面是服务日志里的 traceback：\n"
                                    "```\n%s\n```\n"
                                    "请输出 JSON 对象，字段：\n"
                                    '{"location":"文件名:行号", "cause":"一句话原因（含真实变量/值）", '
                                    '"fix":"具体修复代码或步骤", '
                                    '"fault_type":"code_error|traffic_surge|dependency_fail|unknown", '
                                    '"key_info":"提取的关键信息（出错行/涉及变量）"}' % tb_text},
    ]


def _try_cloud(tb_text):
    """① 云端大模型。返回诊断 dict 或 None（失败 -> 降级）。
    为什么是"顺序找第一个有 key 的"：多模型分工里分析模型优先 deepseek，
    kimi 没配 key 时复用 deepseek；两者都不可用返回 None 走本地/离线。"""
    provider = cu.resolve_provider(CLOUD_ANALYZE_ALIASES[0])
    if not (provider and provider.get("api_key")):
        provider = cu.resolve_provider(CLOUD_ANALYZE_ALIASES[1])
    if not (provider and provider.get("api_key")):
        provider = cu.any_available_provider()
    if not provider:
        return None
    content = cu.llm_chat(_diag_prompt(tb_text), max_tokens=1500,
                          provider=provider, timeout=20)
    data = _parse_llm_json(content)
    if not data:
        return None
    cause = (data.get("cause") or "").strip()
    fix = (data.get("fix") or "").strip()
    location = (data.get("location") or "").strip()
    if not (cause and fix):
        return None
    return {"location": location, "cause": cause, "fix": fix,
            "fault_type": data.get("fault_type", "unknown"),
            "key_info": data.get("key_info", ""), "provider": provider.get("name")}


def _try_local(tb_text):
    """② 本地模型（Ollama）。返回诊断 dict 或 None（没装/没启动 -> 降级）。"""
    local_provider = {"name": "local-ollama", "model": LOCAL_MODEL,
                      "base_url": LOCAL_BASE_URL, "api_key": LOCAL_API_KEY}
    content = cu.llm_chat(_diag_prompt(tb_text), max_tokens=400,
                          provider=local_provider, timeout=8)
    data = _parse_llm_json(content)
    if not data:
        return None
    cause = (data.get("cause") or "").strip()
    fix = (data.get("fix") or "").strip()
    location = (data.get("location") or "").strip()
    if not (cause and fix):
        return None
    return {"location": location, "cause": cause, "fix": fix,
            "fault_type": data.get("fault_type", "unknown"),
            "key_info": data.get("key_info", ""), "provider": LOCAL_MODEL}


def diagnose(tb_text, force_local=False):
    """统一诊断接口：三级降级（cloud -> local -> offline）。
    任何一步失败自动降级，绝不抛异常。
    force_local=True 时跳过云端直接试本地（--local 参数，方便测试）。"""
    tb_text = tb_text or ""
    # 先取离线结果兜底（不调用任何外部资源，保证最后一定有东西返回）
    offline = None
    try:
        import offline_fallback as of
        offline = of.diagnose(tb_text)
    except Exception:
        offline = None
    if not offline or not isinstance(offline, dict):
        offline = {"location": "unknown", "cause": "无法解析该故障",
                   "fix": "请人工查看日志", "mode": "offline"}

    # ① 云端
    if not force_local:
        r = _try_cloud(tb_text)
        if r is not None:
            return {**r, "mode": "cloud",
                    "error_type": offline.get("error_type"),
                    "code_line": offline.get("code_line")}

    # ② 本地
    r = _try_local(tb_text)
    if r is not None:
        return {**r, "mode": "local",
                "error_type": offline.get("error_type"),
                "code_line": offline.get("code_line")}

    # ③ 离线规则库
    return {**offline, "mode": "offline"}


# ---- 自测（--selftest）-----------------------------------------------------
_SAMPLE_TB = """Traceback (most recent call last):
  File "target_app.py", line 141, in order
    price = stock / count
ZeroDivisionError: division by zero"""


def _patch_diag(cloud_fn, local_fn):
    """自测辅助：临时替换模块级 _try_cloud/_try_local（不走网络），返回旧值。"""
    import sys
    mod = sys.modules[__name__]
    old = (mod._try_cloud, mod._try_local)
    mod._try_cloud = cloud_fn
    mod._try_local = local_fn
    return old


def _restore_diag(old):
    import sys
    mod = sys.modules[__name__]
    mod._try_cloud, mod._try_local = old


def run_selftest():
    """自测要点：① 降级链顺序正确（cloud 失败 -> local 失败 -> offline）；
    ② --local 跳过云端；③ offline 分支一定兜得住；④ 绝不抛异常。"""
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1. 注入"云端/本地都失败" -> 必须落到 offline 且不抛异常
    old = _patch_diag(lambda tb: None, lambda tb: None)
    try:
        r = diagnose(_SAMPLE_TB)
        check("三级降级: cloud/local 都失败时兜底 offline", r.get("mode") == "offline",
              f"mode={r.get('mode')}")
        check("offline 结果含 location/cause/fix", bool(r.get("location"))
              and bool(r.get("cause")) and bool(r.get("fix")), str(r)[:80])
    finally:
        _restore_diag(old)

    # 2. 云端可用时 mode=cloud（此处只验"注入成功后走 cloud"）
    old = _patch_diag(lambda tb: {"location": "target_app.py:141", "cause": "count=0 导致除零",
                                  "fix": "加判断 if count==0", "fault_type": "code_error",
                                  "key_info": "count=0"}, lambda tb: None)
    try:
        r = diagnose(_SAMPLE_TB)
        check("云端可用时 mode=cloud", r.get("mode") == "cloud", f"mode={r.get('mode')}")
    finally:
        _restore_diag(old)

    # 3. force_local=True 时跳过云端（云端再成功也不走）
    old = _patch_diag(lambda tb: {"location": "x", "cause": "x", "fix": "x"},
                      lambda tb: {"location": "target_app.py:141", "cause": "本地原因",
                                  "fix": "本地修复", "fault_type": "code_error", "key_info": ""})
    try:
        r = diagnose(_SAMPLE_TB, force_local=True)
        check("--local 强制走本地", r.get("mode") == "local" and r.get("cause") == "本地原因",
              f"mode={r.get('mode')}")
    finally:
        _restore_diag(old)

    # 4. force_local 时本地也失败 -> 依然落到 offline
    old = _patch_diag(lambda tb: {"location": "x", "cause": "x", "fix": "x"}, lambda tb: None)
    try:
        r = diagnose(_SAMPLE_TB, force_local=True)
        check("--local 且本地不可用时兜底 offline", r.get("mode") == "offline",
              f"mode={r.get('mode')}")
    finally:
        _restore_diag(old)

    # 5. 空输入/异常输入不崩
    r = diagnose("")
    check("空输入不抛异常", isinstance(r, dict) and r.get("mode") in
          ("cloud", "local", "offline"), f"mode={r.get('mode')}")

    # 6. 真实离线规则库可直接调用（不依赖注入）
    import offline_fallback as of
    off = of.diagnose(_SAMPLE_TB)
    check("offline_fallback.diagnose 兼容", off.get("mode") == "offline"
          and bool(off.get("fix")), str(off)[:80])

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\ndiagnose selftest 结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


def main():
    parser = argparse.ArgumentParser(description="统一诊断接口（三级降级）")
    parser.add_argument("--local", action="store_true", help="强制走本地模型")
    parser.add_argument("--selftest", action="store_true", help="运行自测")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(run_selftest())
    tb = _SAMPLE_TB
    print("诊断日志：")
    print(tb)
    print("\n诊断结果：")
    print(json.dumps(diagnose(tb, force_local=args.local),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
