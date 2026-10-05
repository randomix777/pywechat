"""摘要卡 —— 只做转述，不做判断。

## 为什么不需要那套契约

客户下单很随便（���「上次那种」「那个黄的」），大部分条目对不上目录里的
任何商品。契约要求填满 sku/quantity，模型只能编，而编出来的 sku 会让商户
照着发错货。

商户要的是**一眼看清这个群在说什么**。他自己知道哪个是苹果哪个是酱油，
不需要软件替他判断。所以这里只有转述。

## 三条不能退让的地方

1. **原话照抄**。客户说「那个黄的」就写「那个黄的」。
2. **说不清就说说不清**。不能猜一个填上。
3. **不写意见**。不判断这是新单还是追加 —— 那是店主的记忆。
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyweixin.summary import (  # noqa: E402
    DEFAULT_SUMMARY_MODEL,
    SUMMARY_SYSTEM_PROMPT,
    Summarizer,
    summarize_messages,
)


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _reply(content: str) -> _Response:
    return _Response({"choices": [{"message": {"content": content}}],
                      "usage": {"cost": 0.0002}})


class DefaultModelTests(unittest.TestCase):
    """默认模型必须是真实验证过的那个。

    一开始我就在脚本里写死了 gpt-4o-mini，而项目实际用的是 DeepSeek ——
    那样验证结论对项目没有价值。模型标识一定得来自真实列表。
    """

    def test_it_defaults_to_the_model_we_actually_validated(self):
        self.assertEqual(DEFAULT_SUMMARY_MODEL, "deepseek/deepseek-v4.1-flash")


class PromptTests(unittest.TestCase):
    """提示词里那三条约束必须都在 —— 它们是这套设计成立的前提。"""

    def test_it_tells_the_model_to_keep_the_customers_words(self):
        self.assertIn("原话", SUMMARY_SYSTEM_PROMPT)
        self.assertIn("那个黄的", SUMMARY_SYSTEM_PROMPT,
                      "必须举这个例子，否则模型会自己替换成商品名")

    def test_it_tells_the_model_not_to_invent(self):
        self.assertIn("不要编造", SUMMARY_SYSTEM_PROMPT)

    def test_it_forbids_opinions(self):
        """模型必须只转述，不给建议。"""
        self.assertIn("建议", SUMMARY_SYSTEM_PROMPT)
        self.assertIn("你只负责转述", SUMMARY_SYSTEM_PROMPT)

    def test_it_asks_for_plain_text_not_json(self):
        self.assertIn("不要 JSON", SUMMARY_SYSTEM_PROMPT)


class RequestShapeTests(unittest.TestCase):
    def setUp(self):
        self.summarizer = Summarizer("https://example.invalid/v1", "k", "m")

    def _body(self):
        with mock.patch("urllib.request.urlopen", return_value=_reply("好")) as opener:
            self.summarizer.summarize([{"sender": "张", "content": "要苹果"}])
        return json.loads(opener.call_args[0][0].data.decode("utf-8"))

    def test_max_tokens_is_always_set(self):
        """不设会被默认成 65536，余额不够直接 402 —— 这是实测踩到的。"""
        self.assertEqual(self._body()["max_tokens"], 2000)

    def test_no_response_format_is_requested(self):
        """摘要不是结构化输出，别去约束 json_schema。"""
        self.assertNotIn("response_format", self._body())


class BehaviourTests(unittest.TestCase):
    def setUp(self):
        self.summarizer = Summarizer("https://example.invalid/v1", "k", "m")

    def _summarize(self, messages, catalog=()):
        with mock.patch("urllib.request.urlopen", return_value=_reply("简报正文")) as opener:
            result = self.summarizer.summarize(messages, catalog)
        return result, json.loads(opener.call_args[0][0].data.decode("utf-8"))

    def test_it_returns_the_text_and_the_cost(self):
        result, _ = self._summarize([{"sender": "张", "content": "要苹果"}])
        self.assertEqual(result["summary"], "简报正文")
        self.assertEqual(result["cost"], 0.0002)
        self.assertIsNone(result["error"])

    def test_an_empty_group_makes_no_call(self):
        """没有消息就不该花钱。"""
        with mock.patch("urllib.request.urlopen") as opener:
            result = self.summarizer.summarize([])
        opener.assert_not_called()
        self.assertEqual(result["summary"], "")
        self.assertEqual(result["message_count"], 0)

    def test_the_catalog_is_handed_over_as_reference_only(self):
        """目录给模型看是让它知道店里卖什么，**不是**让它去挑编码。"""
        _, body = self._summarize(
            [{"sender": "张", "content": "要苹果"}],
            [{"name": "红富士苹果", "unit": "箱", "sku": "P-APL"}])
        payload = json.loads(body["messages"][1]["content"])
        # 目录下的键带着“仅供参考”的提示
        self.assertTrue(any("仅供参考" in k for k in payload),
                        f"没在给模型的上下文里找到提示：{list(payload)}")
        # sku 不该出现在给模型的上下文里
        self.assertNotIn("P-APL", json.dumps(payload, ensure_ascii=False))

    def test_an_http_error_is_reported_not_raised(self):
        """商户界面不该因为一次网络失败就崩掉。"""
        import urllib.error
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError("u", 402, "no credit", {}, None)):
            result = self.summarizer.summarize([{"sender": "张", "content": "要苹果"}])
        self.assertEqual(result["error"], "HTTP 402")
        self.assertEqual(result["summary"], "", "失败时不要返回半截文字冒充结果")


class HelperTests(unittest.TestCase):
    def test_the_function_wrapper_is_the_same_thing(self):
        with mock.patch("urllib.request.urlopen", return_value=_reply("正文")):
            result = summarize_messages([{"sender": "张", "content": "要苹果"}],
                                        base_url="https://example.invalid/v1",
                                        api_key="k", model="m")
        self.assertEqual(result["summary"], "正文")


if __name__ == "__main__":
    unittest.main()