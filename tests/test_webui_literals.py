"""网关浏览器页面的 JS 字面量回归测试。

出过的事：``_js_literal`` 只转义不加引号，页面吐出来是
``csrf=65457cf2…;`` —— 浏览器整段 script 语法错误，改密码/确认设备的
按钮全死，点了没反应，连报错都没有。

这里只渲染字符串断言，不起网关、不碰微信。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyweixin import webui  # noqa: E402


class JsLiteralTests(unittest.TestCase):
    def test_value_is_a_quoted_js_string(self):
        self.assertEqual(webui._js_literal("abc123"), '"abc123"')

    def test_quotes_inside_are_escaped_not_breaking_out(self):
        rendered = webui._js_literal('a"b</script>')
        self.assertTrue(rendered.startswith('"') and rendered.endswith('"'))
        self.assertNotIn("</script>", rendered)

    def test_first_login_page_assigns_csrf_without_syntax_error(self):
        page = webui.render_first_login("测试店", "65457cf2")
        self.assertIn('csrf="65457cf2";', page)

    def test_device_confirm_page_quotes_code_and_csrf(self):
        page = webui.render_device_confirm("测试店", "BCDF-2345", "收银电脑", "tok123")
        self.assertIn('code="BCDF-2345"', page)
        self.assertIn('csrf="tok123"', page)


if __name__ == "__main__":
    unittest.main()
