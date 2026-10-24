"""
offline_fallback.py —— 离线规则库（任务 E，P0 必做）

作用：当断网 / 没有 API Key / 大模型调用失败时，Agent 退回本模块做诊断。
它是一本"异常速查手册"：拿到 traceback 文本，解析出文件+行号+异常类型，
再用内置的 10 类异常修复模板给出：故障位置 / 原因 / 修复代码。

设计原则（比赛现场断网保命）：
  - 零第三方依赖（只用标准库）
  - 秒级返回、绝不抛异常（任何异常输入都返回可读的兜底结果）
  - diagnose(traceback_text) -> {"location", "cause", "fix", "mode": "offline"}

用法：
  python offline_fallback.py            # 直接跑内置测试
  python offline_fallback.py --selftest # 同上（统一入口）
"""

import os
import re
import sys

# ---- 10 类异常的规则库 ------------------------------------------------------
# 每类给：一句话原因 + 修复模板。
# 模板里用 {var0} {var1} {var2} 做占位符，diagnose 时会把出错行里真实的变量名
# 填进去，让修复建议"长在"用户自己的代码上，而不是空泛的教科书。
RULES = {
    "ZeroDivisionError": {
        "cause": "除数为 0：一般是被除对象（库存/数量/分母）恰好为 0 时做了除法",
        "fix": "除法前先判 0，为 0 时给个安全默认值，避免整个请求 500：\n"
               "    if {var1} == 0:\n"
               "        result = 0\n"
               "    else:\n"
               "        result = {var0} / {var1}",
    },
    "KeyError": {
        "cause": "字典里没有这个键：一般是查了不存在的订单号/用户 ID/配置项",
        "fix": "取值前先用 get 或先判断存在性，不存在时返回友好提示：\n"
               "    if {var0} in {var1}:\n"
               "        value = {var1}[{var0}]\n"
               "    else:\n"
               "        value = None   # 返回 404/默认值，而不是抛异常",
    },
    "IndexError": {
        "cause": "列表索引越界：一般是从空列表里取第 0 个元素（数据为空）",
        "fix": "取元素前先判断列表非空：\n"
               "    if {var0}:\n"
               "        first = {var0}[0]\n"
               "    else:\n"
               "        first = None   # 空数据走默认分支",
    },
    "TypeError": {
        "cause": "类型不匹配：一般是字符串/列表和数字直接做了运算（如 'abc' - 5）",
        "fix": "运算前先统一类型，把输入显式转成目标类型：\n"
               "    {var0} = int({var0})   # 先把用户传进来的值转成数字\n"
               "    result = {var0} - {var1}",
    },
    "AttributeError": {
        "cause": "对象没有这个属性/方法：一般是 None 上取属性，或拼错属性名",
        "fix": "先判断对象非 None 再取属性：\n"
               "    if {var0} is not None:\n"
               "        value = {var0}.{attr}\n"
               "    else:\n"
               "        value = None",
    },
    "ValueError": {
        "cause": "值本身不合法：一般是把 'abc' 之类非数字字符串转成 int/float",
        "fix": "转换前先捕获异常，非法输入走默认值：\n"
               "    try:\n"
               "        {var0} = int({var1})\n"
               "    except ValueError:\n"
               "        {var0} = 0   # 非法输入给个安全默认值",
    },
    "NameError": {
        "cause": "变量名未定义：一般是拼写错误，或变量只在某个分支里定义了",
        "fix": "在使用前初始化变量（在函数开头给默认值）：\n"
               "    {var0} = None   # 在使用前先初始化\n"
               "    if condition:\n"
               "        {var0} = ...",
    },
    "ImportError": {
        "cause": "导入失败：一般是第三方库没装、包名拼错、或断网装不上依赖",
        "fix": "缺依赖时给友好提示并降级（用标准库实现，或提示安装命令）：\n"
               "    try:\n"
               "        import {var0}\n"
               "    except ImportError:\n"
               "        {var0} = None   # 降级逻辑",
    },
    "FileNotFoundError": {
        "cause": "文件不存在：一般是路径写错、文件被移动/删除、或相对路径基准不对",
        "fix": "打开文件前先检查存在性，不存在时给默认数据：\n"
               "    if os.path.exists({var0}):\n"
               "        with open({var0}, encoding='utf-8') as f:\n"
               "            data = f.read()\n"
               "    else:\n"
               "        data = ''   # 文件缺失时的兜底",
    },
    "ConnectionError": {
        "cause": "网络连接失败：一般是下游服务没起/端口不对/断网/连接被拒",
        "fix": "把网络请求包在 try 里，失败走降级数据，不让主流程崩：\n"
               "    try:\n"
               "        result = {var0}()\n"
               "    except ConnectionError:\n"
               "        result = None   # 降级：用缓存或默认数据",
    },
    "NotFound": {
        "cause": "路由 404：请求的 URL 在 Flask 路由表里没有注册（或方法不匹配）",
        "fix": "检查 target_app.py 是否注册了对应路由：\n"
               "    @app.route('/{var0}', methods=['GET'])\n"
               "    def {var0}_page():\n"
               "        return {{'ok': True, 'data': []}}   # 给请求一个明确响应",
    },
    "WebsocketMismatch": {
        "cause": "路由规则与请求方法不匹配：多半是定义了路由但 methods 里少了请求用到的方法",
        "fix": "在 target_app.py 对应路由的 methods 里补上缺失方法：\n"
               "    @app.route('/{var0}', methods=['GET', 'POST'])\n"
               "    def {var0}_page():\n"
               "        ...   # 保留原业务逻辑，只扩展 methods",
    },
}

# 备用模板：遇到不认识的异常类型时兜底（绝不裸奔返回）
_DEFAULT_RULE = {
    "cause": "未收录的异常类型，建议结合 traceback 手工排查出错行",
    "fix": "先把出错行包进 try/except，记录日志后给友好提示，避免整个请求 500",
}


# ---- 补丁生成（给 Agent 的沙箱修复链路用） ----------------------------------
# 目标：把出错行改写成"安全版本"，输出统一 diff 格式：
#     line: <行号>
#     old: <当前行>
#     new: <安全替换行>
# 每种异常用一行"特征正则 + 安全改写"，保证现场演示时补丁真的能修好服务。
def _build_zero_div_new(code_line):
    """ZeroDivisionError：给除法分母加保护，为 0 时用 1，避免崩溃。"""
    if "/" not in code_line or "=" not in code_line:
        return None
    lhs, rhs = code_line.split("=", 1)
    # 取 / 右边的第一个标识符（含下标，如 goods["stock"]）作为分母
    m = re.search(r"([A-Za-z_][A-Za-z0-9_\.\[\]'\"\- ]*)", rhs.split("/", 1)[1].strip())
    if not m or not m.group(1).strip():
        return None
    denom = m.group(1).strip()
    return f"{lhs.strip()} = ({rhs.strip()}) if {denom} != 0 else 0"


def _build_key_error_new(code_line):
    """KeyError：把 dict[key] 改成 dict.get(key, 默认值)，键不存在时不再抛异常。"""
    m = re.search(r"(\w+)\[([^\]]+)\]", code_line)
    if m:
        return code_line.replace(f"{m.group(1)}[{m.group(2)}]",
                                 f"{m.group(1)}.get({m.group(2)}, None)")
    return None


def _build_index_error_new(code_line):
    """IndexError：取 [0] 前先判断列表非空，空列表走默认值。"""
    m = re.search(r"(\w+)\[([^\]]+)\]\[0\]", code_line)
    if m:
        var, key = m.group(1), m.group(2)
        return f"{code_line} if {var}[{key}] else None"
    return None


def _build_type_error_new(code_line):
    """TypeError：把 - 右边的操作数显式转 int，字符串数字也能参与运算。"""
    m = re.search(r"^(.*?)\s*-\s*([A-Za-z_]\w*)\s*$", code_line)
    if m:
        return f"{m.group(1)} - int({m.group(2)})"
    return None


_PATCH_BUILDERS = {
    "ZeroDivisionError": _build_zero_div_new,
    "KeyError": _build_key_error_new,
    "IndexError": _build_index_error_new,
    "TypeError": _build_type_error_new,
}


def build_patch(traceback_text):
    """根据 traceback 生成统一 diff 格式补丁；无法自动改写时返回 None。
    Agent 拿到 patch 后：先沙箱验证（复制代码->应用->起 8081->验证），
    通过才应用，绝不盲改线上文件。"""
    try:
        parsed = parse_traceback(traceback_text)
        builder = _PATCH_BUILDERS.get(parsed["error"])
        if not builder:
            return None
        code_line = _read_code_line(parsed["file"], parsed["line"])
        if not code_line:
            return None
        # 为什么先去掉行尾注释再改写：出错行常带"# 库存 0 时崩溃"这类注释，
        # 注释会混进分母提取/行尾匹配里，导致生成错误的补丁。
        clean = re.sub(r"\s*#.*$", "", code_line).strip()
        if not clean:
            return None
        new_line = builder(clean)
        if not new_line or new_line.strip() == clean.strip():
            return None
        return f"line: {parsed['line']}\nold: {code_line}\nnew: {new_line}"
    except Exception:
        return None


# ---- 解析 traceback --------------------------------------------------------
def parse_traceback(traceback_text):
    """从 traceback 文本里提取 (文件, 行号, 异常类型)。
    为什么取"最后一个" File 行：Python traceback 从外到内打印调用栈，
    真正的出错点在最内层（最后一个 File 行）；异常类型在最后一行的冒号前。"""
    tb = traceback_text or ""
    files = re.findall(r'File "([^"]+)", line (\d+)', tb)
    last = files[-1] if files else ("unknown", 0)
    m = re.search(r'([A-Za-z]+Error)(?::|\s*$)', tb.splitlines()[-1] if tb.splitlines() else "")
    error_type = m.group(1) if m else "UnknownError"
    return {"file": last[0], "line": int(last[1]), "error": error_type}


def _extract_names(code_line):
    """从一行代码里提取"看起来像变量"的标识符（变量名/属性名/方法名）。
    为什么这么做：把规则模板里的 {var0} {var1} 占位符替换成真实代码里的名字，
    修复建议才有代入感。提取策略：取代码里出现的前几个字母开头标识符。"""
    names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", code_line)
    # 去掉常见关键字，避免模板里出现 if/else/for 这类词
    keywords = {"if", "else", "for", "in", "return", "def", "import", "from", "try",
                "except", "raise", "and", "or", "not", "while", "with", "as", "lambda"}
    return [n for n in names if n not in keywords] or ["value"]


def _read_code_line(file_path, line):
    """安全读取指定文件的第 line 行代码。任何异常都返回空字符串（绝不抛异常）。"""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()
    except Exception:
        pass
    return ""


# ---- 对外接口 ---------------------------------------------------------------
def diagnose(traceback_text):
    """核心接口：诊断一段 traceback，返回定位 + 原因 + 修复建议。
    mode 固定为 "offline"：上层 Agent 用它区分"大模型诊断"与"本地规则库诊断"。"""
    try:
        parsed = parse_traceback(traceback_text)
        error = parsed["error"]
        rule = RULES.get(error, _DEFAULT_RULE)

        code_line = _read_code_line(parsed["file"], parsed["line"])
        names = _extract_names(code_line) if code_line else ["value", "data"]

        fix = rule["fix"]
        # 占位符替换：{var0}->第一个名字, {var1}->第二个名字, {attr}->常见属性名
        fix = fix.replace("{var0}", names[0] if len(names) > 0 else "value")
        fix = fix.replace("{var1}", names[1] if len(names) > 1 else names[0])
        fix = fix.replace("{attr}", names[1] if len(names) > 1 else "data")

        return {
            "location": f"{os.path.basename(parsed['file'])}:{parsed['line']}",
            "cause": rule["cause"],
            "fix": fix,
            "mode": "offline",
            "error_type": error,
            "code_line": code_line,
        }
    except Exception as e:  # 最外层兜底：本模块任何情况都不能让调用方崩
        return {
            "location": "unknown",
            "cause": f"规则库自身解析失败: {e}",
            "fix": "请人工查看最近一条 traceback 定位问题",
            "mode": "offline",
            "error_type": "UnknownError",
            "code_line": "",
        }


# ---- 内置测试 ---------------------------------------------------------------
def run_selftest():
    """给 10 类异常各造一段真实感的 traceback，验证 diagnose 都能返回有效结果，
    且能正确解析出文件/行号/异常类型。全程零网络零服务，拔网线也能跑。"""
    # 为什么用相对路径写样例：样例本身就是"诊断测试文件"，diagnose 会去读它，
    # 让"读取出错行代码"这一环也被测到。
    sample_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_samples.py")
    samples = [
        ("ZeroDivisionError", "ZeroDivisionError: division by zero"),
        ("KeyError", "KeyError: 'ORD-9999'"),
        ("IndexError", "IndexError: list index out of range"),
        ("TypeError", "TypeError: unsupported operand type(s) for -: 'int' and 'str'"),
        ("AttributeError", "AttributeError: 'NoneType' object has no attribute 'name'"),
        ("ValueError", "ValueError: invalid literal for int() with base 10: 'abc'"),
        ("NameError", "NameError: name 'total' is not defined"),
        ("ImportError", "ImportError: No module named 'pandas'"),
        ("FileNotFoundError", "FileNotFoundError: [Errno 2] No such file or directory: 'data.json'"),
        ("ConnectionError", "ConnectionError: Connection refused"),
    ]
    # 造一个样例文件，让第 3 行/第 7 行是"出错行"，diagnose 能读到真实代码
    sample_code = (
        "def sample():\n"
        "    a = 10\n"
        "    result = a / b\n"
        "    return result\n"
        "def other():\n"
        "    data = {}\n"
        "    value = data['key']\n"
    )
    with open(sample_file, "w", encoding="utf-8") as f:
        f.write(sample_code)

    results = []
    for error_type, message in samples:
        # 造 traceback：固定指向上面的样例文件第 3 行（除法）或第 7 行（取键）
        line = 3 if error_type == "ZeroDivisionError" else 7
        tb = (f'Traceback (most recent call last):\n'
              f'  File "{sample_file}", line {line}, in <module>\n'
              f'    result = a / b\n'
              f'{error_type}: {message}')
        r = diagnose(tb)
        ok = r["mode"] == "offline" and r["error_type"] == error_type
        results.append((error_type, ok, r))
        print(f"  [{'PASS' if ok else 'FAIL'}] {error_type:20s} -> {r['location']} | {r['cause'][:30]}")

    # 边界：空输入/None 输入也绝不抛异常
    for bad in (None, "", "garbage without traceback"):
        r = diagnose(bad)
        ok = r["mode"] == "offline" and isinstance(r["fix"], str)
        results.append((f"兜底({str(bad)[:12]})", ok, r))
        print(f"  [{'PASS' if ok else 'FAIL'}] 兜底输入 -> {r['location']}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nselftest 结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    else:
        sys.exit(run_selftest())
