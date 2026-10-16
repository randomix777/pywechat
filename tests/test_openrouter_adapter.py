"""OpenRouter 适配器。

## 这些测试盯的是什么

OpenRouter 和 DeepSeek 都是 OpenAI 兼容接口，**传输层几乎一样**。所以这个
适配器最容易出的错不是连不上，而是**说了假话还看不出来**：

* 计费记到 ``deepseek`` 名下 —— 运营看报表时完全发现不了；
* 把 DeepSeek 的模型清单当成 OpenRouter 的可��模型 —— 界面上会出现
  根本没在 OpenRouter 开过的模型名；
* 套用 DeepSeek 的下线名单去判断 OpenRouter 的模型 —— 运营会去处理一个
  不存在的问题。

所以这里一半的测试在验证「**不说不知道的事**」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from business_agent.backends.deepseek import DeepSeekBackend  # noqa: E402
from wechat_ai_assistant.opencode import MERCHANT_MESSAGES, ParseOutcome  # noqa: E402
from wechat_ai_assistant.openrouter_adapter import (  # noqa: E402
    API_KEY_ENV,
    DEFAULT_BASE_URL,
    MODEL_ENV,
    OpenRouterAdapter,
    api_key_from_env,
    build_from_env,
)


def _adapter(**kwargs):
    """用假后端建适配器，不发任何真实请求。"""
    backend = DeepSeekBackend(
        api_key="sk-test", model=kwargs.pop("model", "test/model"),
        base_url=kwargs.pop("base_url", DEFAULT_BASE_URL),
    )
    return OpenRouterAdapter(api_key="sk-test", backend=backend, **kwargs)


class IdentityTests(unittest.TestCase):
    def test_the_provider_is_not_called_deepseek(self):
        """计费与统计认这个名字。沿用 deepseek 会让报表错到无人察觉。"""
        self.assertEqual(_adapter().provider_id, "openrouter")

    def test_the_two_providers_can_coexist(self):
        """环境变量分开命名，否则配了 OpenRouter 会顶掉 DeepSeek。"""
        self.assertNotEqual(API_KEY_ENV, "WECHAT_AI_DEEPSEEK_API_KEY")
        self.assertNotEqual(MODEL_ENV, "WECHAT_AI_DEEPSEEK_MODEL")


class HonestyTests(unittest.TestCase):
    """这一组是重点：验证适配器**不编造**。"""

    def test_no_deepseek_model_list_leaks_into_the_report(self):
        report = _adapter().probe()
        self.assertNotIn("models_documented", report,
                         "那是 DeepSeek 官方的清单，不能当成 OpenRouter 的")
        self.assertNotIn("retired_models", report,
                         "DeepSeek 的下线名单对 OpenRouter 无意义")

    def test_it_does_not_claim_to_know_which_models_are_available(self):
        """OpenRouter 能用哪些模型取决于账户开通，本地没有权威事实。"""
        providers = _adapter().list_providers()
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["models"], [],
                         "列出来的模型名都是猜的，不如不列")
        self.assertIn("未内置清单", providers[0]["models_note"])

    def test_it_does_not_apply_the_deepseek_retirement_list(self):
        """套用 DeepSeek 的下线名单，会让运营去处理一个不存在的问题。"""
        adapter = _adapter(model="some/model/nobody/knows")
        self.assertEqual(adapter.model_warning, "")

    def test_the_report_says_pricing_is_not_frozen(self):
        """OpenRouter 按美元、每次成本不同 —— 报告里必须写明。"""
        report = _adapter().probe()
        self.assertIn("美元", report["pricing_note"])
        self.assertIn("冻结", report["pricing_note"])


class AttributionHeaderTests(unittest.TestCase):
    """归因请求头：没配就不发，而不是发一个编造的域名。"""

    def test_unset_by_default(self):
        adapter = _adapter()
        self.assertEqual(adapter.referer, "")
        self.assertEqual(adapter.title, "")

    def test_configured_values_are_kept(self):
        adapter = _adapter(referer="https://shop.example", title="接单助手")
        self.assertEqual(adapter.referer, "https://shop.example")
        self.assertEqual(adapter.title, "接单助手")

    def test_blank_strings_are_treated_as_unset(self):
        adapter = _adapter(referer="   ", title="")
        self.assertEqual(adapter.referer, "")
        self.assertEqual(adapter.title, "")


class ConfigurationTests(unittest.TestCase):
    def test_a_missing_key_is_not_an_error(self):
        """装配阶段要依次试多个上游，缺一个密钥不该让程序起不来。"""
        self.assertEqual(api_key_from_env({}), "")
        self.assertIsNone(build_from_env(env={}))

    def test_a_present_key_builds_the_adapter(self):
        adapter = build_from_env(env={API_KEY_ENV: " sk-x ", MODEL_ENV: "vendor/model"})
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.base_url, DEFAULT_BASE_URL)
        self.assertEqual(adapter._backend.model, "vendor/model")

    def test_the_base_url_can_be_overridden(self):
        adapter = build_from_env(env={
            API_KEY_ENV: "sk-x",
            "WECHAT_AI_OPENROUTER_BASE_URL": "https://gateway.internal/v1",
        })
        self.assertEqual(adapter.base_url, "https://gateway.internal/v1")

    def test_an_empty_key_is_refused_loudly(self):
        """构造时没有密钥就该立刻报错，而不是等到发请求才失败。"""
        with self.assertRaises(ValueError):
            OpenRouterAdapter(api_key="")


class SessionContractTests(unittest.TestCase):
    """契约方法要齐全 —— 网关不区分后端，照常调用它们。"""

    def test_the_lifecycle_methods_exist_and_are_safe(self):
        adapter = _adapter()
        self.assertTrue(adapter.create_session("job-1").startswith("orr_"))
        self.assertTrue(adapter.abort("s"))
        self.assertTrue(adapter.delete_session("s"))

    def test_reconcile_returns_none_rather_than_guessing(self):
        """没有上游会话可查。返回 None 会走人工对账，
        编一个"没跑过"的结论则会先退钱、再发现其实成功了。"""
        self.assertIsNone(_adapter().reconcile("s"))


class RoutingGuardTests(unittest.TestCase):
    def test_it_refuses_to_switch_providers_silently(self):
        """任务冻结了路由，指向别的供应商时必须拒绝，不能偷偷换模型。"""
        adapter = _adapter()
        with self.assertRaises(Exception) as caught:
            adapter.run_parse("s", {}, "job-1", 0.0, provider_id="deepseek")
        self.assertEqual(str(caught.exception), MERCHANT_MESSAGES["no_agent"])

    def test_a_cancelled_job_never_reaches_the_upstream(self):
        adapter = _adapter()
        with self.assertRaises(Exception):
            adapter.run_parse("s", {}, "job-1", 0.0, cancel_check=lambda: True)


if __name__ == "__main__":
    unittest.main()