"""放行判据不能只看「本次读到几条」。

## 出过什么事

界面自相矛盾：按钮放出来了，一点却弹「还没有读到消息，请先点开始读取」。

原因是有**两处**各自判断放行，而且都只看 ``result.total_messages``：

* ``_render_read_state`` 决定「下一步」按钮显不显示
* ``_go_next_from_history`` 决定点了之后放不放行

更新时发现没有新消息，``total_messages`` 就是 0 —— 但库里可能已经积累
了几百条。于是第一处（我改过了）把按钮放出来，第二处（还是老判据）
把它拦回去。商户刚读完一大群，被告知「还没有读到消息」。

摘要那个按钮卡的是同一个坑。

## 现在的判据

统一走 ``_has_messages_for``：本次读到了 → 有；本次 0 条但库里已有 → **也算有**。
真的什么都没有时才拦 —— 那才是死路。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MERCHANT_UI = ROOT / "src/pyweixin/merchant_ui.py"






if __name__ == "__main__":
    unittest.main()