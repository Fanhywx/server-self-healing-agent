"""
target_app.py —— 靶子服务（会被故障搞挂的"真实业务"）

这是比赛演示里的"受害者"：一个真实可用的 Flask 订单服务。
默认状态下完全正常：商品列表 / 下单 / 订单查询 都能走通。
它内置 5 个 bug 开关（BUG1-BUG5），开关默认全关 = 服务健康。
故障只在"边界数据"上发生（查不存在的订单号、买库存为 0 的商品……），
这样演示时能讲成："一个真实用户请求触发了代码里本来就有的缺陷"，
而不是"我们偷偷注入了一个 bug"。

用法：
  python target_app.py                # 默认多线程模式（性能好）
  python target_app.py --no-threaded  # 单线程模式（压测时容易被压垮，用于演示）\n\n\n@app.route('/', defaults={'path': ''})\n@app.route('/<path:path>')\ndef index(path):\n    return jsonify({'status': 'ok', 'path': path})
  python target_app.py --port 8081    # 沙箱验证专用端口
  python target_app.py --selftest     # 自测业务与 5 个 bug 开关

为什么写在 /health /inject /reset 这些接口：Agent 与演示控制台都要用它来判断
服务死活（/health）、制造故障（/inject/1）、恢复服务（/reset）。
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
import traceback

from flask import Flask, jsonify, render_template_string, request, session

# ---- 常量 ---------------------------------------------------------------
# 为什么用 __file__ 定位目录：代码可能在任意目录运行（演示现场经常拷贝），
# 日志文件 app.log 和数据 events.jsonl 必须和代码放一起，用绝对路径最稳。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(APP_DIR, "app.log")
ACCOUNTS_FILE = os.path.join(APP_DIR, "accounts.json")   # 商家账号（与 dashboard 共用）

# ---- 真实业务数据（默认全部正常） -----------------------------------------
# G004 库存为 0、G000 缺 price 字段：这两个是"边界数据"，
# 配合 BUG2/BUG4 在特定请求下触发故障。
GOODS = {
    "G001": {"name": "机械键盘", "price": 199, "stock": 10},
    "G002": {"name": "无线鼠标", "price": 89, "stock": 50},
    "G003": {"name": "USB 扩展坞", "price": 129, "stock": 30},
    "G004": {"name": "显示器支架", "price": 259, "stock": 0},   # 库存为 0 的边界商品
    "G000": {"name": "神秘商品", "price": None, "stock": 5},     # 缺 price 字段的边界商品
}

ORDERS = {}          # order_id -> 订单字典
_order_seq = 1       # 下单序号，用来生成订单号
_leak_pool = []      # 内存泄漏池（BUG5 专用，留着不释放）

# 5 个 bug 开关，默认全关（服务健康）
BUG_FLAGS = {i: False for i in range(1, 6)}

app = Flask(__name__)
# 为什么设 secret_key：商家登录态用 Flask session 存"当前登录账号"，
# session 需要服务端密钥签名，否则 cookie 被篡改就能伪装成任意商家。
# 密钥写死在这里是演示用途；现场若要更安全可改成环境变量读取。
app.secret_key = os.environ.get("SECRET_KEY", "ucloud-demo-2026-secret")

# ---- 日志配置 ------------------------------------------------------------
# 为什么把 Flask 的日志同时写进 app.log：Agent 的 watcher 只盯着这一个文件
# 增量读取，发现 [ERROR]/Traceback 就自动触发诊断。统一出口最简单。
_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
app.logger.addHandler(_handler)
app.logger.setLevel(logging.INFO)


# ---- 小工具 --------------------------------------------------------------
def _log_info(msg):
    """业务日志，供演示回放"故障是怎么发生的"。"""
    app.logger.info(msg)


def _hash_pwd(pwd):
    """密码做 SHA-256 哈希再存盘：即使 accounts.json 被翻到也不能直接看到明文密码。"""
    return hashlib.sha256((pwd or "").encode("utf-8")).hexdigest()


def _load_accounts():
    """读商家账号表（与 dashboard.py 共用同一文件，任何情况不抛异常）。
    返回 {"shopadmin": {"password": "sha256..."}, ...}"""
    if not os.path.exists(ACCOUNTS_FILE):
        return {}
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_account(account, password):
    """新增/更新商家账号（dashboard 指令窗口与 /merchant/register 共用）。
    原子写：先写临时文件再 os.replace，避免两个进程同时写坏文件。"""
    accounts = _load_accounts()
    accounts[account] = {"password": _hash_pwd(password), "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACCOUNTS_FILE)
        return True
    except Exception:
        return False


def _verify_login(account, password):
    """校验账号密码（sha256 比对）。"""
    accounts = _load_accounts()
    rec = accounts.get(account)
    if not rec:
        return False
    return rec.get("password") == _hash_pwd(password)


def _maybe_leak():
    """BUG5：内存泄漏。每次下单往 _leak_pool 里塞 1MB 字符串且永不释放。
    为什么这么设计：内存泄漏是"自然发生"的故障，不需要任何人注入，
    压测跑几分钟后服务会越来越慢直到 OOM，正好演示"Agent 自己发现并修复"。 """
    if BUG_FLAGS[5]:
        _leak_pool.append("X" * 1024 * 1024)
        _log_info(f"[BUG5] 内存泄漏池大小: {len(_leak_pool)} MB")


# ---- 业务页面（客户端 / 商家端，任务 G 的前端） ----------------------------
# 为什么用 render_template_string 而不是单独 html 文件：比赛要求"代码能直接跑、
# 文件越少越好"，把两个前端页面内嵌进 target_app.py，拷贝整个目录就能演示。
CLIENT_HTML = """
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>小店 - 客户端</title>
<style>body{font-family:sans-serif;max-width:720px;margin:40px auto}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px}
button{padding:8px 18px;margin-top:8px;cursor:pointer}</style></head>
<body>
<h2>小店 · 商品列表</h2>
<table><tr><th>编号</th><th>名称</th><th>价格</th><th>库存</th><th>操作</th></tr>
{% for gid, g in goods.items() %}
<tr><td>{{ gid }}</td><td>{{ g.name }}</td><td>{{ g.price }}</td>
<td>{{ g.stock }}</td><td><button onclick="buy('{{ gid }}', 1)">下单</button></td></tr>
{% endfor %}</table>
<h3>下单结果</h3><div id="result">（点击下单后显示结果）</div>
<script>
async function buy(goods, count) {
  const res = await fetch('/order', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({goods: goods, count: count})});
  document.getElementById('result').innerText = 'HTTP ' + res.status + ' : ' + await res.text();
}
</script>
</body></html>
"""

# 商家端页面：未登录 -> 登录表单（页面右侧）；已登录 -> 商家管理台。
# 样式与客户端保持一致（浅色简洁风格），登录框单独放右侧一栏，
# 演示时"未登录先看到登录框、登录成功跳管理台"的流程一目了然。
MERCHANT_LOGIN_HTML = """
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>小店 - 商家登录</title>
<style>body{font-family:sans-serif;max-width:820px;margin:40px auto;color:#222}
.wrap{display:flex;gap:40px;align-items:flex-start}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:24px;flex:1}
.login{max-width:300px;background:#f7f9fb}
h2{margin-top:0}input{display:block;width:100%;padding:8px;margin:8px 0 12px;
border:1px solid #ccc;border-radius:4px;box-sizing:border-box}
button{background:#1a73e8;color:#fff;border:none;padding:10px 18px;border-radius:4px;cursor:pointer}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px}
.msg{color:#d93025;font-size:14px}</style></head>
<body>
<div class="wrap">
<div class="card">
<h2>小店 · 商家管理台</h2>
<div class="msg">{{ msg }}</div>
<h3>商品与库存</h3>
<table><tr><th>编号</th><th>名称</th><th>价格</th><th>库存</th></tr>
{% for gid, g in goods.items() %}
<tr><td>{{ gid }}</td><td>{{ g.name }}</td><td>{{ g.price }}</td><td>{{ g.stock }}</td></tr>
{% endfor %}</table>
<form action="/merchant/update" method="post">
  商品编号 <input name="goods_id" required style="width:100px;display:inline-block">  新价格 <input name="price" style="width:100px;display:inline-block">
  新库存 <input name="stock" style="width:100px;display:inline-block">  <button>保存</button>
</form>
<h3>订单列表</h3>
<table><tr><th>订单号</th><th>商品</th><th>数量</th><th>金额</th></tr>
{% for oid, o in orders.items() %}
<tr><td>{{ oid }}</td><td>{{ o.items }}</td><td>{{ o.count }}</td><td>{{ o.total }}</td></tr>
{% endfor %}</table>
</div>
<div class="card login">
<h2>商家登录</h2>
<p>请先登录后管理商品与订单</p>
<form action="/merchant/login" method="post">
  账号 <input name="account" required>
  密码 <input name="password" type="password" required>
  <button>登录</button>
</form>
</div>
</div>
</body></html>
"""

MERCHANT_HTML = """
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>小店 - 商家管理台</title>
<style>body{font-family:sans-serif;max-width:820px;margin:40px auto;color:#222}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:24px}
h2{margin-top:0}input{padding:8px;margin:4px;border:1px solid #ccc;border-radius:4px}
button{background:#1a73e8;color:#fff;border:none;padding:10px 18px;border-radius:4px;cursor:pointer}
.logout{float:right;text-decoration:none;color:#1a73e8;font-size:14px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px}</style></head>
<body>
<div class="card">
<h2>小店 · 商家管理台 <a class="logout" href="/merchant/logout">退出登录</a></h2>
<h3>商品与库存</h3>
<table><tr><th>编号</th><th>名称</th><th>价格</th><th>库存</th></tr>
{% for gid, g in goods.items() %}
<tr><td>{{ gid }}</td><td>{{ g.name }}</td><td>{{ g.price }}</td><td>{{ g.stock }}</td></tr>
{% endfor %}</table>
<form action="/merchant/update" method="post">
  商品编号 <input name="goods_id" required style="width:100px">  新价格 <input name="price" style="width:100px">
  新库存 <input name="stock" style="width:100px">  <button>保存</button>
</form>
<h3>订单列表</h3>
<table><tr><th>订单号</th><th>商品</th><th>数量</th><th>金额</th></tr>
{% for oid, o in orders.items() %}
<tr><td>{{ oid }}</td><td>{{ o.items }}</td><td>{{ o.count }}</td><td>{{ o.total }}</td></tr>
{% endfor %}</table>
</div>
</body></html>
"""


# ---- 路由：业务接口 --------------------------------------------------------
@app.route("/")
def index():
    """客户端页面：商品列表 + 下单按钮。"""
    return render_template_string(CLIENT_HTML, goods=GOODS)


@app.route("/merchant")
def merchant():
    """商家端页面：未登录 -> 登录表单（右侧登录框）；已登录 -> 商家管理台。
    登录态由 Flask session 里的 merchant_account 决定。"""
    if session.get("merchant_account"):
        return render_template_string(MERCHANT_HTML, goods=GOODS, orders=ORDERS)
    # 未登录也展示商品/订单的只读视图 + 右侧登录框，页面不会空
    return render_template_string(MERCHANT_LOGIN_HTML, goods=GOODS, orders=ORDERS, msg="")


@app.route("/merchant/login", methods=["POST"])
def merchant_login():
    """商家登录：读 accounts.json 验证账号密码，成功则写入 session。"""
    account = (request.form.get("account") or "").strip()
    password = request.form.get("password") or ""
    if not account or not password:
        return render_template_string(MERCHANT_LOGIN_HTML, goods=GOODS, orders=ORDERS,
                                      msg="账号和密码不能为空")
    if not _verify_login(account, password):
        return render_template_string(MERCHANT_LOGIN_HTML, goods=GOODS, orders=ORDERS,
                                      msg="账号或密码错误")
    session["merchant_account"] = account
    _log_info(f"商家登录成功: {account}")
    return render_template_string(MERCHANT_HTML, goods=GOODS, orders=ORDERS)


@app.route("/merchant/register", methods=["POST"])
def merchant_register():
    """商家注册（JSON）：供驾驶舱指令窗口/脚本调用。
    为什么 dashboard 也直接写同一个 accounts.json：两者是不同进程，
    统一以本地文件为"唯一事实源"，接口与直写文件保持同一套哈希与原子写逻辑。"""
    data = request.get_json(silent=True) or request.form
    account = (data.get("account") or "").strip()
    password = data.get("password") or ""
    if not account or not password:
        return jsonify({"ok": False, "msg": "账号和密码不能为空"}), 400
    if len(account) < 2:
        return jsonify({"ok": False, "msg": "账号至少 2 个字符"}), 400
    ok = _save_account(account, password)
    if not ok:
        return jsonify({"ok": False, "msg": "写入 accounts.json 失败"}), 500
    _log_info(f"商家账号创建成功: {account}")
    return jsonify({"ok": True, "msg": f"商家账号 {account} 创建成功"})


@app.route("/merchant/logout")
def merchant_logout():
    """退出登录：清 session 回登录页。"""
    session.pop("merchant_account", None)
    return render_template_string(MERCHANT_LOGIN_HTML, goods=GOODS, orders=ORDERS, msg="已退出登录")


@app.route("/goods")
def goods_list():
    """JSON 商品列表（供脚本/控制台调用）。"""
    return jsonify(GOODS)


@app.route("/order", methods=["POST"])
def place_order():
    """下单接口。BUG3/BUG2/BUG5 都挂在这里：
    - BUG3：count 传字符串时，库存-数量 直接报 TypeError
    - BUG2：买库存为 0 的商品（G004）时，算折扣价要除以库存 -> ZeroDivisionError
    - BUG5：每次下单泄漏 1MB
    """
    data = request.get_json(silent=True) or {}
    goods_id = str(data.get("goods", ""))
    count = data.get("count", 1)

    if goods_id not in GOODS:
        return jsonify({"error": "商品不存在"}), 404

    goods = GOODS[goods_id]
    _maybe_leak()  # BUG5

    if BUG_FLAGS[3]:
        # 模拟"脏数据"：count 是字符串，库存-字符串 直接 TypeError
        stock_after = goods["stock"] - int(count)     # 修复：显式转换 count 为 int
    else:
        stock_after = goods["stock"] - int(count)

    if BUG_FLAGS[2]:
        # 模拟"买库存为 0 的商品"：算折扣价时除以库存 -> ZeroDivisionError
        discount = goods["price"] / goods["stock"]    # 库存 0 时崩溃
    else:
        discount = 0

    if stock_after < 0:
        return jsonify({"error": "库存不足"}), 400

    global _order_seq
    oid = f"ORD-{_order_seq:04d}"
    _order_seq += 1
    # 为什么把缺 price 的商品按 0 元处理：G000 是"缺字段"的边界商品，
    # 正常版要能下单（否则它一出现就是 500，Agent 永远等不到 BUG4 的 IndexError）。
    price = goods["price"] if goods["price"] is not None else 0
    total = price * int(count)
    # 缺 price 时 items 保持空列表：BUG4 就是靠"查神秘商品订单详情"触发 IndexError
    items = [f"{goods['name']}x{count}"] if goods["price"] is not None else []
    ORDERS[oid] = {"id": oid, "goods": goods_id, "count": count, "total": total, "items": items}
    GOODS[goods_id]["stock"] = stock_after
    _log_info(f"下单成功 {oid}: {goods['name']}x{count}")
    return jsonify({"order_id": oid, "total": total})


@app.route("/orders/<oid>")
def order_detail(oid):
    """订单查询。BUG1：查不存在的订单号 -> KeyError；BUG4：神秘商品订单 items 为空 -> IndexError。"""
    if BUG_FLAGS[1]:
        if oid not in ORDERS: return jsonify({"error": "Order not found"}), 404; order = ORDERS[oid]
    else:
        if oid not in ORDERS:
            return jsonify({"error": "订单不存在"}), 404
        order = ORDERS[oid]

    if BUG_FLAGS[4]:
        first = order["items"][0] if order["items"] else None
        _log_info(f"订单首条目: {first}")
    return jsonify(order)


@app.route("/orders")
def orders_list():
    """订单列表（商家端数据源）。"""
    return jsonify(list(ORDERS.values()))


@app.route("/merchant/update", methods=["POST"])
def merchant_update():
    """商家改价格/库存。"""
    gid = request.form.get("goods_id", "")
    if gid not in GOODS:
        return jsonify({"error": "商品不存在"}), 404
    if request.form.get("price"):
        GOODS[gid]["price"] = int(request.form["price"])
    if request.form.get("stock"):
        GOODS[gid]["stock"] = int(request.form["stock"])
    _log_info(f"商家更新 {gid}: {GOODS[gid]}")
    return jsonify({"ok": True})


@app.route("/slow")
def slow():
    """慢接口：模拟依赖超时/慢查询，压测演示"服务被拖垮"用。"""
    time.sleep(2)
    return jsonify({"status": "slow-ok"})


# ---- 路由：运维/演示接口（供 Agent 与控制台调用） ----------------------------
@app.errorhandler(Exception)
def handle_unhandled(e):
    """全局异常兜底：任何未捕获异常 -> 把完整 traceback 写进 app.log + 返回 500。
    为什么必须有这一层：Agent 的 watcher 只盯着 app.log 增量读，而 Flask 默认把
    traceback 打到 stderr（不进文件）；没有这层，Agent 就永远"看不到"故障，
    整个自愈链路的第一步（alert）就断了。"""
    tb_text = traceback.format_exc()
    app.logger.error("Unhandled exception:\n%s", tb_text)
    return jsonify({"error": "internal error", "type": type(e).__name__}), 500


@app.route("/health")
def health():
    """存活探针：Agent 判断服务是死是活、沙箱验证是否通过都用它。"""
    return jsonify({"status": "up", "time": time.strftime("%H:%M:%S")})


@app.route("/inject/<int:bug_id>")
def inject(bug_id):
    """打开第 bug_id 个 bug 开关（1-5）。为什么单独做开关而不是一上来就崩：
    演示时"先展示一片绿，再模拟一个真实请求，灯变红"，节奏完全可控。"""
    if bug_id < 1 or bug_id > 5:
        return jsonify({"error": "bug_id 必须是 1-5"}), 400
    BUG_FLAGS[bug_id] = True
    _log_info(f"[INJECT] 打开 BUG{bug_id} -> {BUG_FLAGS}")
    return jsonify({"ok": True, "bug": bug_id, "flags": BUG_FLAGS})


@app.route("/reset")
def reset():
    """恢复服务：关闭所有 bug 开关 + 清空泄漏池。演示"修复后一片绿"用。"""
    for i in range(1, 6):
        BUG_FLAGS[i] = False
    _leak_pool.clear()
    _log_info("[RESET] 服务已恢复")
    return jsonify({"ok": True, "flags": BUG_FLAGS})


# ---- 父进程守护（防残留） --------------------------------------------------
# 为什么要有这个：main.py 用子进程方式拉起靶子服务，如果现场有人直接关掉
# main 的控制台窗口，靶子进程会变成孤儿一直占着 8080。这里每 2 秒检查一次
# 父进程（--parent-pid）是否还活着，父进程没了就自杀退出，保证无进程残留。
def _parent_alive(pid):
    """Windows 下判断进程 pid 是否存活。
    为什么必须声明 restype=c_void_p：OpenProcess 返回 64 位句柄，ctypes 默认
    按 32 位 int 截断，句柄会被截成 0 或错值，导致"父进程死了仍判活着"，
    target 永不自杀 -> 端口残留（比赛现场翻车点）。"""
    import ctypes
    if pid <= 0:
        return True  # 没传父进程 id 就不守护
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    # 二次确认：GetExitCodeProcess 返回 259(STILL_ACTIVE) 才真正算活着，
    # 避免"进程刚被 terminate、进程对象还在系统里"的窗口期误判
    code = ctypes.c_ulong(0)
    ok = kernel32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
    kernel32.CloseHandle(ctypes.c_void_p(h))
    if not ok:
        return False
    return code.value == 259


def _watch_parent(parent_pid):
    """守护线程：父进程退出后本进程也退出（防止现场残留进程占端口）。"""
    while _parent_alive(parent_pid):
        time.sleep(2)
    _log_info(f"父进程 {parent_pid} 已退出，靶子服务自动退出（防残留）")
    os._exit(0)


# ---- 自测（--selftest） -----------------------------------------------------
def run_selftest():
    """不起真实端口，用 Flask 自带的 test_client 模拟请求验证：
    正常业务能走通 + 5 个 bug 开关在边界数据上分别产生 500。"""
    client = app.test_client()
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1. 正常流程：查商品 -> 下单 -> 查订单，全链路 200
    r = client.get("/goods")
    check("正常: 商品列表", r.status_code == 200)
    r = client.post("/order", json={"goods": "G001", "count": 2})
    check("正常: 下单 G001", r.status_code == 200, r.get_data(as_text=True)[:60])
    oid = r.get_json().get("order_id")
    r = client.get(f"/orders/{oid}")
    check("正常: 查询订单", r.status_code == 200)
    r = client.get("/orders/999999")
    check("正常: 查不存在订单->404(不崩)", r.status_code == 404)

    # 2. BUG1 KeyError：查不存在的订单号
    client.get("/inject/1")
    r = client.get("/orders/999999")
    check("BUG1: KeyError -> 500", r.status_code == 500)

    # 3. BUG2 ZeroDivisionError：买库存为 0 的商品
    client.get("/inject/2")
    r = client.post("/order", json={"goods": "G004", "count": 1})
    check("BUG2: ZeroDivisionError -> 500", r.status_code == 500)

    # 4. BUG3 TypeError：count 传字符串
    client.get("/inject/3")
    r = client.post("/order", json={"goods": "G002", "count": "abc"})
    check("BUG3: TypeError -> 500", r.status_code == 500)

    # 5. BUG4 IndexError：神秘商品订单 items 为空
    client.get("/reset")
    client.get("/inject/4")
    r = client.post("/order", json={"goods": "G000", "count": 1})
    oid2 = r.get_json().get("order_id") if r.status_code == 200 else None
    if oid2:
        r = client.get(f"/orders/{oid2}")
        check("BUG4: IndexError -> 500", r.status_code == 500)
    else:
        check("BUG4: IndexError -> 500", False, "下单未成功")

    # 6. BUG5 内存泄漏：打开后泄漏池增长
    client.get("/inject/5")
    before = len(_leak_pool)
    client.post("/order", json={"goods": "G001", "count": 1})
    check("BUG5: 内存泄漏增长", len(_leak_pool) > before,
          f"泄漏池 {before} -> {len(_leak_pool)} MB")

    # 7. 恢复
    client.get("/reset")
    r = client.post("/order", json={"goods": "G001", "count": 1})
    check("恢复: 关掉 bug 后正常下单", r.status_code == 200)

    # 8. 商家登录链路（用临时 accounts.json，不污染真实账号表）
    global ACCOUNTS_FILE
    _old_accounts = ACCOUNTS_FILE
    test_accounts = os.path.join(APP_DIR, "temp", "test_accounts.json")
    if os.path.exists(test_accounts):
        os.remove(test_accounts)
    ACCOUNTS_FILE = test_accounts
    _save_account("shopadmin", "pass123")
    check("商家: 创建账号（哈希存盘）",
          _verify_login("shopadmin", "pass123") and not _verify_login("shopadmin", "wrong"))
    r = client.get("/merchant")
    check("商家: 未登录显示登录框（右侧）", r.status_code == 200
          and "商家登录" in r.get_data(as_text=True))
    r = client.post("/merchant/login", data={"account": "shopadmin", "password": "pass123"})
    check("商家: 登录成功显示管理台", r.status_code == 200
          and "商家管理台" in r.get_data(as_text=True))
    r = client.post("/merchant/login", data={"account": "shopadmin", "password": "bad"})
    check("商家: 密码错误提示", "账号或密码错误" in r.get_data(as_text=True))
    r = client.post("/merchant/register", json={"account": "shop2", "password": "p2"})
    check("商家: 注册接口", r.status_code == 200 and r.get_json().get("ok") is True)
    ACCOUNTS_FILE = _old_accounts

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nselftest 结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


def main():
    parser = argparse.ArgumentParser(description="靶子服务（会被搞挂的真实业务）")
    parser.add_argument("--threaded", action="store_true", default=None,
                        help="多线程模式（性能好，默认）")
    parser.add_argument("--no-threaded", action="store_true",
                        help="单线程模式（压测容易被压垮，演示用）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="父进程 PID：父进程退出则本进程自动退出（防残留）")
    parser.add_argument("--selftest", action="store_true", help="运行自测")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())

    # 默认 threaded=True；显式 --no-threaded 时单线程
    threaded = not args.no_threaded
    if args.parent_pid > 0:
        # 由 main.py 拉起时开启守护，确保 main 退出后本进程不留残
        threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True).start()
    _log_info(f"target_app 启动: port={args.port} threaded={threaded}")
    app.run(host="0.0.0.0", port=args.port, threaded=threaded, debug=False)


if __name__ == "__main__":
    main()
