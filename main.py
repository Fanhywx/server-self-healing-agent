"""
main.py —— 统一入口（任务 A，P0 必做）

一条命令把整个"自愈链路"跑起来：
  1. 启动/复用靶子服务 target_app.py（8080）
  2. watcher 线程每秒增量读 app.log，发现新故障（Traceback）自动触发 Agent
  3. Agent：alert(故障) -> locate(定位) -> reason(读上下文+分类) -> patch(生成补丁)
            -> 沙箱验证(8081 隔离复验) -> apply(应用并重启服务)
  4. 每步写 events.jsonl（六 stage 数据契约），控制台 dashboard 实时展示

大模型优先，失败自动退离线规则库（offline_fallback）：断网也照样能修。

用法：
  python main.py                # 全自动演示（默认多线程靶子）
  python main.py --threaded     # 靶子多线程（性能好）
  python main.py --no-threaded  # 靶子单线程（配合压测演示"被压垮"）
  python main.py --selftest     # 离线自测：4 类异常定位全部正确
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

# 为什么用 sys.path 插入：main.py 和其余模块同目录，直接 import 公共模块
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import common_utils as cu
import offline_fallback as of
from classifier import classify
import diagnose as dg      # 任务 H：三级降级统一诊断接口
import self_heal as sh     # 任务 H：多模型分工 + 闭环自我修正

TARGET_FILE = os.path.join(BASE_DIR, "target_app.py")
APP_LOG = cu.APP_LOG
EVENTS_FILE = cu.EVENTS_FILE
BACKUP_FILE = os.path.join(BASE_DIR, "temp", "original_target_app.py")


def backup_original_target():
    """第一次运行时把干净的 target_app.py 备份到 temp。
    为什么必须备份：Agent 修好一个 bug 会真实改写 target_app.py，
    演示完想再注入同一个 bug 就触发不了了；备份让演示可以反复排练
    （python main.py --restore-target 一键还原）。"""
    if os.path.exists(BACKUP_FILE):
        return
    os.makedirs(os.path.dirname(BACKUP_FILE), exist_ok=True)
    try:
        with open(TARGET_FILE, encoding="utf-8") as src, \
                open(BACKUP_FILE, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print(f"[main] 已备份原始靶子服务: {BACKUP_FILE}")
    except Exception as e:
        print(f"[main] 备份失败: {e}")


# ==================== 1. 靶子服务管理 ====================
class TargetManager:
    """负责启动/复用/重启靶子服务（8080），并保证退出时无进程残留。"""

    def __init__(self, threaded=True):
        self.threaded = threaded
        self.proc = None  # 由本程序启动的子进程；自己起的不归我管

    def ensure_running(self):
        """8080 已经有服务就直接复用（比如选手手动起了 target_app）；
        没有则由本程序拉起一个子进程。"""
        if cu.service_alive(8080):
            print("[main] 检测到 8080 已有靶子服务，直接复用")
            return
        print(f"[main] 启动靶子服务 target_app.py (port=8080, threaded={self.threaded})")
        self.proc = subprocess.Popen(
            [sys.executable, TARGET_FILE, "--port", "8080",
             "--parent-pid", str(os.getpid()),
             "--threaded" if self.threaded else "--no-threaded"],
            cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # 轮询等它起来（最多 15 秒）
        deadline = time.time() + 15
        while time.time() < deadline:
            if cu.service_alive(8080):
                print("[main] 靶子服务已就绪")
                return
            if self.proc.poll() is not None:
                print("[main] 靶子服务启动失败，退出码:", self.proc.returncode)
                sys.exit(1)
            time.sleep(0.5)
        print("[main] 靶子服务启动超时，请检查端口占用")
        sys.exit(1)

    def restart(self):
        """应用补丁后重启服务，让修复真正生效。"""
        if self.proc is None:
            # 服务不是本程序启动的：先找到 8080 占用进程并结束，再接管拉起，
            # 否则补丁只写入文件、线上仍跑旧代码，自愈“假成功”。
            self._kill_port_process(8080)
            self.ensure_running()
            return
        print("[main] 重启靶子服务以应用修复...")
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True)
            except Exception:
                pass
        self.proc = None
        self.ensure_running()

    def _kill_port_process(self, port):
        """跨平台结束占用指定端口的进程（用于接管外部启动的靶子服务）。"""
        try:
            if sys.platform.startswith("win"):
                r = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=10)
                for line in r.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.split()
                        pid = parts[-1]
                        subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                       capture_output=True)
                        print(f"[main] 已结束外部靶子进程 PID={pid}")
            else:
                r = subprocess.run(
                    ["ss", "-tlnp"], capture_output=True, text=True, timeout=10)
                import re
                for line in r.stdout.splitlines():
                    if f":{port}" in line:
                        m = re.search(r"pid=(\d+)", line)
                        if m:
                            os.kill(int(m.group(1)), signal.SIGKILL)
                            print(f"[main] 已结束外部靶子进程 PID={m.group(1)}")
        except Exception as e:
            print(f"[main] 结束外部靶子进程失败: {e}")

    def shutdown(self):
        """程序退出时清理自己拉起的子进程，保证无残留。"""
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True)
                except Exception:
                    pass
            self.proc = None


# ==================== 2. 日志 watcher ====================
class LogWatcher(threading.Thread):
    """每秒增量读 app.log，发现新 Traceback 就触发 Agent。
    关键防翻车点：用文件 offset 增量读取 + 5 秒冷却，绝不重复触发。"""

    def __init__(self, log_path, on_error):
        super().__init__(daemon=True)
        self.log_path = log_path
        self.on_error = on_error
        # 初始 offset 指向文件末尾：只关心"之后"发生的新故障
        self.offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        self.buffer = []          # 最近 200 行，用来拼完整 traceback
        self.last_trigger = 0.0   # 上次触发时间（冷却用）
        self.seen_tbs = []        # 最近触发过的 traceback 特征（去重用）
        self.stop_flag = False

    @staticmethod
    def _tb_mark(tb_text):
        """traceback 的特征：出错文件+行号+异常类型。为什么不用全文 hash：
        errorhandler 每次写日志的缩进/路径前缀可能略有差异，用核心特征判断
        "是不是同一个故障"更稳。"""
        import hashlib
        lines = [l for l in tb_text.splitlines() if "File \"" in l or l.strip().endswith("Error")]
        key = "\n".join(lines[-3:]) or tb_text[:200]
        return hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()

    def _is_duplicate(self, tb_text):
        """同一故障（同文件同行同异常）只在 60 秒内处理一次。
        为什么必须去重：一次故障的 traceback 可能被日志系统分多批写出，
        或现场反复请求同一接口，不去重会让 Agent 对同一个 bug 连续修两次，
        events.jsonl 里出现"第二次 apply 失败"，演示看起来像翻车。"""
        mark = self._tb_mark(tb_text)
        now = time.time()
        # 清理超过 60 秒的旧记录
        self.seen_tbs = [(m, t) for m, t in self.seen_tbs if now - t < 60]
        if any(m == mark for m, _ in self.seen_tbs):
            return True
        self.seen_tbs.append((mark, now))
        return False

    def run(self):
        while not self.stop_flag:
            try:
                new_text, self.offset = cu.read_log_tail(self.log_path, self.offset)
                if new_text:
                    for line in new_text.splitlines():
                        self.buffer.append(line)
                    self.buffer = self.buffer[-200:]

                    if "Traceback" in new_text:
                        # 为什么 sleep 1 秒：traceback 尾部（异常类型行）通常是
                        # 下一个日志批次才写完，等一拍再收集，能拿到完整证据。
                        time.sleep(1)
                        _, self.offset = cu.read_log_tail(self.log_path, self.offset)
                        if self.offset > 0:
                            with open(self.log_path, "r", encoding="utf-8",
                                      errors="replace") as f:
                                f.seek(self.offset)
                                tail = f.read()
                            for line in tail.splitlines():
                                self.buffer.append(line)
                            self.buffer = self.buffer[-200:]

                        # 从 buffer 里取最近一个 Traceback 到末尾
                        idxs = [i for i, l in enumerate(self.buffer) if "Traceback" in l]
                        if idxs:
                            tb = "\n".join(self.buffer[idxs[-1]:])
                            now = time.time()
                            # 冷却 5 秒 + 去重：同一瞬间的重复批次、同一故障
                            # 反复触发都只处理一次，避免对同一个 bug 连续修两次
                            if now - self.last_trigger >= 5 and not self._is_duplicate(tb):
                                self.last_trigger = now
                                self.on_error(tb)
            except Exception as e:
                print(f"[watcher] 异常（不影响继续监听）: {e}")
            time.sleep(1)

    def stop(self):
        self.stop_flag = True


# ==================== 3. Agent 核心 ====================
class RepairAgent:
    """自愈 Agent：看到 traceback 就执行完整修复链路，每步写 events.jsonl。"""

    def __init__(self, manager, force_local=False):
        self.manager = manager
        self.force_local = force_local   # 任务 H：--local 强制走本地模型（方便测试）
        # 成本统计（写 verify 事件用）
        self.start_ts = 0

    def handle_traceback(self, tb_text):
        print("\n==================== 检测到故障 ====================")
        self.start_ts = time.time()
        cu.write_event("alert", error=self._error_name(tb_text))

        # ---- 定位 ----
        parsed = cu.parse_traceback(tb_text)
        file_name = os.path.basename(parsed["file"])
        line_no = parsed["line"]
        print(f"[locate] {file_name}:{line_no}  异常={parsed['error']}")
        cu.write_event("locate", file=file_name, line=line_no,
                       error=parsed["error"], tb=tb_text[-120:])

        # ---- 分类（决定处置路线）----
        cls = classify(tb_text)
        print(f"[classify] {cls['type']} | 证据: {cls['evidence'][0]}")
        cu.write_event("reason", cls_type=cls["type"], evidence=cls["evidence"][0])

        if cls["type"] != "code_error":
            print(f"[reason] 非代码缺陷（{cls['type']}），不生成补丁，走 {cls['action']}")
            return

        # ---- 读出错行上下文 ----
        context = self._read_context(parsed["file"], line_no)
        print(f"[reason] 出错行上下文:\n{context}")

        # ---- 诊断（任务 H：三级降级 cloud -> local -> offline）----
        # 统一接口 diagnose(log)，mode 写入 reason 事件并在控制台展示；
        # 上层无感知，永远只调 diagnose()。失败自动降级不抛异常。
        diag = dg.diagnose(tb_text, force_local=self.force_local)
        print(f"[reason] 诊断完成 (mode={diag.get('mode')}) | 原因: {diag.get('cause')}")
        cu.write_event("reason", file=file_name, line=line_no,
                       context=context[-200:], mode=diag.get("mode"),
                       cause=diag.get("cause"), location=diag.get("location"))

        # ---- 多模型分工 + 闭环自我修正（任务 H）----
        # 分析模型判型 + 代码模型生成补丁 + 沙箱验证(8081) + 失败重试(≤3)。
        # 每次重试写 events（stage=patch, attempt 序号）；超过上限返回
        # {"ok": false, "reason": "超过重试上限"}；任一模型不通自动降级用
        # 同一模型 -> 本地 -> 离线规则库兜底。
        loop = sh.run_repair(
            tb_text, parsed=parsed, context=context,
            force_local=self.force_local,
            write_event=lambda stage, **kw: cu.write_event(stage, **kw),
            check_paths=["/health"])
        cost = round(time.time() - self.start_ts, 1)
        verify = loop.get("verify") or {}

        if not loop["ok"]:
            print(f"[verify] 闭环未完成: {loop.get('reason')} (mode={loop.get('mode')})")
            cu.write_event("verify", ok=False, seconds=cost, tokens=0,
                           details=verify.get("results", []),
                           reason=loop.get("reason"), mode=loop.get("mode"))
            return

        patch, mode = loop["patch"], loop["mode"]
        print(f"[patch] 补丁生成 (mode={mode}, 尝试 {loop['attempts']} 次):\n{patch}")
        cu.write_event("verify", ok=True, seconds=cost, tokens=0,
                       details=verify.get("results", []),
                       attempts=loop["attempts"], mode=mode)

        # ---- 应用补丁并重启 ----
        app_res = cu.apply_patch_to_file(TARGET_FILE, patch)
        if not app_res["ok"]:
            print(f"[apply] 应用补丁失败: {app_res['msg']}")
            cu.write_event("apply", ok=False, msg=app_res["msg"])
            return
        self.manager.restart()
        ok = cu.service_alive(8080)
        print(f"[apply] 补丁已应用并重启，服务状态: {'正常' if ok else '异常'}")
        cu.write_event("apply", ok=ok, line=app_res.get("line"), file="target_app.py",
                       mode=mode)
        print("==================== 修复完成 ====================\n")

    # ---- 内部方法 ----
    @staticmethod
    def _error_name(tb_text):
        parsed = cu.parse_traceback(tb_text)
        return parsed["error"]

    @staticmethod
    def _read_context(file_path, line_no, before=3, after=2):
        """读出错行附近代码（前 3 后 2），让 Agent/大模型看到现场。"""
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            return ""
        start = max(0, line_no - 1 - before)
        end = min(len(lines), line_no + after)
        ctx = []
        for i in range(start, end):
            mark = ">>" if i == line_no - 1 else "  "
            ctx.append(f"{mark} {i + 1}: {lines[i].rstrip()}")
        return "\n".join(ctx)

    def _gen_patch(self, parsed, context, tb_text):
        """优先大模型生成补丁；没 key / 断网 / 模型异常 -> 退离线规则库。"""
        self._used_llm = False
        llm_patch = self._llm_patch(parsed, context)
        if llm_patch:
            self._used_llm = True
            return llm_patch
        print("[patch] 大模型不可用（无 key/断网/失败），退回离线规则库")
        return of.build_patch(tb_text)

    @staticmethod
    def _llm_patch(parsed, context):
        """让大模型按统一 diff 格式输出补丁。为什么 prompt 里给死格式：
        模型输出格式化了才能被沙箱安全应用，否则只能展示不能自动修。"""
        prompt = (
            "你是服务端故障修复专家。下面是一段 Python 代码出错现场，"
            f"出错文件 {os.path.basename(parsed['file'])} 第 {parsed['line']} 行，"
            f"异常类型 {parsed['error']}。\n上下文代码：\n{context}\n\n"
            "请只输出一个统一格式的补丁（不要任何解释），格式如下：\n"
            f"line: {parsed['line']}\nold: <第 {parsed['line']} 行的原始代码>\n"
            "new: <修复后的单行代码>\n"
            "要求：修复后该行不会抛同样的异常，且保持缩进一致。"
        )
        content = cu.llm_chat([{"role": "user", "content": prompt}],
                              max_tokens=1500, timeout=30)
        # 简单校验：至少包含 line:/old:/new: 三个关键字才采信
        if content and all(k in content for k in ("line:", "old:", "new:")):
            return content.strip()
        return None


# ==================== 4. 主流程 ====================
def main():
    parser = argparse.ArgumentParser(description="服务端故障自愈 Agent 统一入口")
    parser.add_argument("--threaded", action="store_true", default=None,
                        help="靶子服务多线程模式（默认）")
    parser.add_argument("--no-threaded", action="store_true",
                        help="靶子服务单线程模式（配合压测演示被压垮）")
    parser.add_argument("--selftest", action="store_true", help="离线自测定位能力")
    parser.add_argument("--local", action="store_true",
                        help="任务 H：强制诊断/补丁走本地模型（Ollama qwen3:8b）")
    parser.add_argument("--restore-target", action="store_true",
                        help="从备份还原靶子服务源码（演示后可反复排练）")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())

    if args.restore_target:
        sys.exit(restore_target())

    print("=" * 50)
    print("服务端故障自愈 Agent 已启动")
    print(f"  events 契约文件: {EVENTS_FILE}")
    print(f"  日志文件: {APP_LOG}")
    print("=" * 50)

    # 启动前先备份干净版，演示才能反复排练
    backup_original_target()

    manager = TargetManager(threaded=not args.no_threaded)
    manager.ensure_running()

    agent = RepairAgent(manager, force_local=args.local)
    watcher = LogWatcher(APP_LOG, agent.handle_traceback)
    watcher.start()
    print("[main] watcher 已启动，每秒监听新故障。Ctrl+C 退出")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[main] 收到 Ctrl+C，退出中...")
    finally:
        watcher.stop()
        manager.shutdown()
        print("[main] 已清理子进程，无残留")


# ==================== 5. 自测（--selftest） ====================
def restore_target():
    """用备份还原 target_app.py，并重启 8080 服务让还原生效。
    为什么单独给还原入口：演示现场希望"修完还能再演一遍"，
    一键还原比手改代码安全得多。"""
    if not os.path.exists(BACKUP_FILE):
        print("[restore] 没有备份文件，无法还原（第一次启动 main.py 才会生成备份）")
        return 1
    try:
        with open(BACKUP_FILE, encoding="utf-8") as src, \
                open(TARGET_FILE, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print(f"[restore] 已从备份还原 target_app.py")
    except Exception as e:
        print(f"[restore] 还原失败: {e}")
        return 1

    # 8080 活着就重启，让还原立即生效
    if cu.service_alive(8080):
        print("[restore] 重启 8080 服务使还原生效...")
        # 精确杀掉占用 8080 的进程（可能是外部起的靶子服务），再重新拉起
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "Get-NetTCPConnection -LocalPort 8080 -State Listen | "
                 "Select-Object -ExpandProperty OwningProcess -Unique | "
                 "ForEach-Object { Stop-Process -Id $_ -Force }"],
                capture_output=True, timeout=10)
        except Exception:
            pass
        time.sleep(1)
    subprocess.Popen(
        [sys.executable, TARGET_FILE, "--port", "8080", "--threaded"],
        cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    print(f"[restore] 完成。服务状态: {'正常' if cu.service_alive(8080) else '异常'}。可重新注入故障开始下一轮演示。")
    return 0


def run_selftest():
    """离线验证 4 类异常定位能力：构造 traceback 样例 -> 走 Agent 的定位 +
    离线规则库补丁生成，断言 4/4 正确。为什么不开服务不调模型：
    自测要稳、要快、拔网线也能跑。"""
    print("main.py --selftest：验证 4 类异常定位 + 离线补丁生成\n")

    # 动态找 target_app.py 里每个 bug 的真实行号（不硬编码，代码改动也不怕）
    src_lines = open(TARGET_FILE, encoding="utf-8").read().splitlines()
    markers = {
        "ZeroDivisionError": ('discount = goods["price"] / goods["stock"]',),
        "KeyError": ("order = ORDERS[oid]",),
        "IndexError": ('first = order["items"][0]',),
        "TypeError": ('stock_after = goods["stock"] - count',),
    }

    def find_line(marker):
        for i, l in enumerate(src_lines, 1):
            if marker in l:
                return i
        return -1

    results = []
    agent = RepairAgent.__new__(RepairAgent)  # 只调静态/独立方法，不启动服务
    for err, (marker,) in markers.items():
        ln = find_line(marker)
        tb = (f'Traceback (most recent call last):\n'
              f'  File "{TARGET_FILE}", line {ln}, in place_order\n'
              f'{err}: test')
        parsed = cu.parse_traceback(tb)
        patch = of.build_patch(tb)
        ok = (os.path.basename(parsed["file"]) == "target_app.py"
              and parsed["line"] == ln
              and parsed["error"] == err
              and patch is not None)
        results.append((err, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {err:20s} 定位 target_app.py:{ln} "
              f"补丁={'有' if patch else '无'}")

    passed = sum(1 for _, ok in results if ok)

    # ---- 任务 H 附加断言 ----
    # ① 诊断三级降级：云/本地都不可用时必须落到离线规则库（mode=offline）
    try:
        dg._try_cloud = lambda *a, **k: None   # 注入：云模型不可用
        dg._try_local = lambda *a, **k: None   # 注入：本地模型不可用
        r_diag = dg.diagnose(
            f'Traceback (most recent call last):\n'
            f'  File "{TARGET_FILE}", line {find_line(markers["ZeroDivisionError"][0])}, in f\n'
            f'ZeroDivisionError: div')
        ok_diag = isinstance(r_diag, dict) and r_diag.get("mode") == "offline" \
            and r_diag.get("cause") and r_diag.get("fix")
        results.append(("Diagnose三级降级->offline", ok_diag))
        print(f"  [{'PASS' if ok_diag else 'FAIL'}] Diagnose三级降级->offline "
              f"mode={r_diag.get('mode')} cause={r_diag.get('cause')!r}")
    except Exception as e:
        results.append(("Diagnose三级降级->offline", False))
        print(f"  [FAIL] Diagnose三级降级->offline 异常: {e}")

    # ② 闭环自我修正离线兜底：真实起 8081 沙箱验证，补丁必须通过
    #    （本地 Ollama 可用则 mode=local，否则离线规则库 mode=offline）
    try:
        r_loop = sh.run_repair(
            f'Traceback (most recent call last):\n'
            f'  File "{TARGET_FILE}", line {find_line(markers["ZeroDivisionError"][0])}, in f\n'
            f'ZeroDivisionError: div',
            parsed=cu.parse_traceback(
                f'Traceback (most recent call last):\n'
                f'  File "{TARGET_FILE}", line {find_line(markers["ZeroDivisionError"][0])}, in f\n'
                f'ZeroDivisionError: div'),
            force_local=True, write_event=lambda *a, **k: None,
            check_paths=["/health"])
        ok_loop = r_loop.get("ok") is True \
            and r_loop.get("mode") in ("local", "offline")
        results.append(("SelfHeal闭环离线兜底", ok_loop))
        print(f"  [{'PASS' if ok_loop else 'FAIL'}] SelfHeal闭环离线兜底 "
              f"ok={r_loop.get('ok')} mode={r_loop.get('mode')} "
              f"attempts={r_loop.get('attempts')}")
    except Exception as e:
        results.append(("SelfHeal闭环离线兜底", False))
        print(f"  [FAIL] SelfHeal闭环离线兜底 异常: {e}")

    passed = sum(1 for _, ok in results if ok)
    print(f"\nselftest 结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    main()
