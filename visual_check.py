"""
visual_check.py —— 视觉审核（任务 H，只在 flow=feature 开发功能链路启用）

用 Playwright 对沙箱页面（http://127.0.0.1:8081）截图，
把截图 + 验收要求发给支持视觉的大模型，模型返回 {"passed": bool, "reason": "..."}，
结果写进验收清单并显示在控制台。

关键约束：
  - 只在 flow=feature 链路调用（修 bug 链路不调用，HTTP 状态码已够）
  - 截图失败 / 模型不可用 -> 直接跳过视觉审核降级为 HTTP 断言，绝不阻塞流程
  - Playwright 装不上（import 失败）程序不能崩、优雅降级
  - playwright 是可选依赖，不写进硬依赖（requirements.txt 仅注释提示）

用法：
  python visual_check.py --selftest
        # 无 Playwright 打印"视觉审核不可用，已降级"且 exit 0
        # 有 Playwright 时输出一张截图 + 判定结果（模型不可用则打印降级原因）
  python visual_check.py --url http://127.0.0.1:8081 --req "验收要求"
        # 手动触发视觉审核（沙箱页面须已在 8081 运行）
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

import common_utils as cu

# 截图默认保存目录（中间产物，不污染项目根目录）
SCREENSHOT_DIR = os.path.join(cu.BASE_DIR, "temp", "visual_shots")


def _playwright_available():
    """Playwright 是否可用（可选依赖，装不上必须优雅降级）。"""
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def screenshot(url, save_dir=None, wait_ms=600):
    """用 Playwright 无头浏览器对页面截图。
    返回 {"ok": True, "path": 绝对路径} 或 {"ok": False, "reason": ...}。
    任何异常（没装 playwright / 页面起不来 / 超时）都不抛到上层。"""
    if not _playwright_available():
        return {"ok": False, "reason": "视觉审核不可用，已降级（Playwright 未安装）"}
    save_dir = save_dir or SCREENSHOT_DIR
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir,
                        "visual_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=8000, wait_until="networkidle")
            page.wait_for_timeout(wait_ms)   # 等页面渲染完再截
            page.screenshot(path=path, full_page=True)
            browser.close()
        if os.path.exists(path):
            return {"ok": True, "path": path}
        return {"ok": False, "reason": "截图文件未生成"}
    except Exception as e:
        return {"ok": False, "reason": "截图失败，已降级为 HTTP 断言：%s" % str(e)[:120]}


def _vision_llm(image_path, requirement):
    """把截图 + 验收要求发给支持视觉的大模型，解析 {"passed","reason"}。
    模型不可用/不支持图片/输出非法 -> 返回 None（上层降级，绝不阻塞）。
    模型选择：优先 config.json providers 里 name 含 vision/vl 的模型，
    没有就退回"当前选中模型"（如 deepseek 不支持视觉会报错 -> 返回 None）。"""
    cfg = cu.load_llm_config()
    provider = None
    for p in cfg.get("providers", []) or []:
        nm = (p.get("name") or "").lower()
        if "vision" in nm or "vl" in nm:
            provider = p
            break
    if not provider:
        llm = cu.resolve_llm()
        if not llm["api_key"]:
            return None
        provider = {"model": llm["model"], "base_url": llm["base_url"] or "",
                    "api_key": llm["api_key"]}
    if not provider.get("api_key"):
        return None
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        data_url = "data:image/png;base64,%s" % b64
        from openai import OpenAI
        client = OpenAI(api_key=provider["api_key"],
                        base_url=provider.get("base_url") or None, timeout=20)
        resp = client.chat.completions.create(
            model=provider.get("model") or provider.get("name"),
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "这是沙箱页面的截图。验收要求：%s\n"
                                         '只输出 JSON：{"passed": true/false, '
                                         '"reason": "一句话判定理由"}' % requirement},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            max_tokens=1500)
        content = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0)) if m else None
        if isinstance(data, dict) and "passed" in data:
            return {"passed": bool(data.get("passed")),
                    "reason": str(data.get("reason", ""))[:200]}
        return None
    except Exception:
        return None


def visual_review(url="http://127.0.0.1:8081", requirement="", save_dir=None):
    """对外统一接口：截图 -> 视觉模型判定。
    返回 {"ok", "passed", "reason", "screenshot", "degraded"}。
    - degraded=True 表示视觉审核不可用/失败，上层应降级为 HTTP 断言（不阻塞）。
    - screenshot 是截图绝对路径（即使判定失败也可能有截图，供演示/控制台展示）。"""
    shot = screenshot(url, save_dir=save_dir)
    if not shot.get("ok"):
        return {"ok": False, "passed": False, "reason": shot.get("reason", "截图失败，已降级"),
                "screenshot": "", "degraded": True}
    judge = _vision_llm(shot["path"], requirement)
    if judge is None:
        return {"ok": False, "passed": False,
                "reason": "视觉模型不可用（当前模型不支持图片或未配置视觉模型），已降级为 HTTP 断言",
                "screenshot": shot["path"], "degraded": True}
    return {"ok": True, "passed": judge["passed"], "reason": judge["reason"],
            "screenshot": shot["path"], "degraded": False}


# ---- 自测（--selftest）-----------------------------------------------------
def run_selftest():
    """验收标准：
    无 Playwright -> 打印"视觉审核不可用，已降级"且 exit 0；
    有 Playwright -> 起临时页面 -> 截图 -> 判定（模型不可用则打印降级原因），
    并保证程序不崩、退出码 0。"""
    if not _playwright_available():
        print("视觉审核不可用，已降级")
        return 0

    print("Playwright 已安装，尝试完整视觉审核链路…")
    # 起一个临时 http 服务器当"沙箱页面"（不依赖 target_app，selftest 更稳）
    tmp_dir = os.path.join(cu.BASE_DIR, "temp", "visual_selftest_page")
    os.makedirs(tmp_dir, exist_ok=True)
    with open(os.path.join(tmp_dir, "index.html"), "w", encoding="utf-8-sig") as f:
        f.write("<html><body><h1>商品列表</h1><p>G001 库存 10 价格 199</p></body></html>")
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8099", "--directory", tmp_dir],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        shot = screenshot("http://127.0.0.1:8099", wait_ms=400)
        if not shot.get("ok"):
            print("截图失败：%s" % shot.get("reason"))
            return 0
        print("截图已生成：%s" % shot["path"])
        judge = _vision_llm(shot["path"], "页面应展示商品列表")
        if judge is None:
            print("视觉模型不可用，已降级为 HTTP 断言（截图保留）")
        else:
            print("判定结果：passed=%s reason=%s" % (judge["passed"], judge["reason"]))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(description="视觉审核（flow=feature 链路可选能力）")
    parser.add_argument("--selftest", action="store_true", help="运行自测")
    parser.add_argument("--url", default="http://127.0.0.1:8081", help="沙箱页面地址")
    parser.add_argument("--req", default="页面应正常显示商品列表", help="验收要求")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(run_selftest())
    print(json.dumps(visual_review(url=args.url, requirement=args.req),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
