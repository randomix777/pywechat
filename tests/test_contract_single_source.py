"""契约必须只有一份定义。

## 为什么需要这条

`items` 的字段清单曾经有**三份副本**：

1. ``order_agent.contract.OUTPUT_SCHEMA``
2. ``orders.OpenAICompatibleParser.schema``（连 intent 枚举都是抄的）
3. ``orders.validate_order_parse_result`` 里的 ``set(item) != {...}``

结果是：为「客户不报 SKU」给 items 加上 ``said``/``brand``/``category`` 之后，
第 1 份变了，第 2、3 份没变 —— 于是**所有**模型输出都被静默拒掉，
而报错只说「结构不符」，没人看得出是哪一边过时了。

这里不检查「值相等」，而是检查**真的引用同一个对象**。
值相等只能证明今天没漂移；引用同一份才能保证明天也不会。

## 提示词也在同一处

提示词里的字段清单与 intent 枚举由 ``OUTPUT_SCHEMA`` 生成。
手写过的那次，提示词说「输出一段固定结构的 JSON 建议」却没给清单，
模型只能猜，真实调用 100% 失败，而单测全绿。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from order_agent.contract import INTENTS, ITEM_KEYS, OUTPUT_KEYS, OUTPUT_SCHEMA  # noqa: E402


class SingleSourceTests(unittest.TestCase):
    def test_the_parser_uses_the_contract_schema_itself(self):
        """不是「碰巧相等」，而是**同一个对象**。"""
        from wechat_ai_assistant.orders import OpenAICompatibleParser

        self.assertIs(OpenAICompatibleParser.schema, OUTPUT_SCHEMA)

    def test_the_parser_uses_the_contract_intents(self):
        from wechat_ai_assistant.orders import OpenAICompatibleParser

        self.assertEqual(
            list(OpenAICompatibleParser.schema["properties"]["intent"]["enum"]),
            list(INTENTS),
        )

    def test_the_schema_and_the_validator_agree_on_item_keys(self):
        from wechat_ai_assistant.orders import ITEM_KEYS as ValidatorKeys

        self.assertEqual(list(OUTPUT_SCHEMA["properties"]["items"]["items"]["required"]),
                         list(ITEM_KEYS))
        self.assertIs(ValidatorKeys, ITEM_KEYS,
                      "校验器用的必须是契约里那一份，不是自己抄的")

    def test_the_prompt_is_generated_from_the_schema(self):
        """提示词里必须出现每个字段名和每个 intent 枚举值。"""
        from order_agent.plugin import SYSTEM_PROMPT

        for key in OUTPUT_KEYS:
            self.assertIn(f'"{key}"', SYSTEM_PROMPT,
                          f"提示词里没有 {key} —— 模型只能靠猜")
        for item_key in ITEM_KEYS:
            self.assertIn(f'"{item_key}"', SYSTEM_PROMPT,
                          f"提示词里没有 item.{item_key}")
        for intent in INTENTS:
            self.assertIn(intent, SYSTEM_PROMPT,
                          f"提示词里没有 intent 枚举值 {intent}")

    def test_the_prompt_points_at_a_context_key_that_exists(self):
        """提示词提到的上下文字段必须真实存在。

        旧提示词让模型查 ``context.product_catalog``，而实际载荷里的字段叫
        ``context.products`` —— 模型被指向一个不存在的字段，只能返回空结果。
        """
        from order_agent.plugin import SYSTEM_PROMPT

        self.assertIn("context.products", SYSTEM_PROMPT)
        self.assertNotIn("context.product_catalog", SYSTEM_PROMPT)

        # 实际载荷里真的有 products
        from wechat_ai_assistant.deepseek_adapter import _build_prompt

        payload = {"messages": [{"role": "user", "content": "x"}],
                   "context": {"products": [{"sku": "S", "name": "苹果", "unit": "箱"}]}}
        package = _build_prompt(payload, "job-1")
        data = getattr(package, "payload", None) or {}
        self.assertIn("products", (data.get("context") or {}))


class NoDuplicateShapeTests(unittest.TestCase):
    """静态检查：别的地方不应该再硬编码同一份 **item** 字段清单。

    只认「sku + quantity + unit + evidence 同时出现」这一组合 —— 那才是 item
    的特征。商品目录是 sku + name + unit + aliases，与 item 形状不同，不应被误伤。

    允许的唯一例外是 ``orders.py`` 的降级兜底（未安装 order_agent 的环境）。
    它必须与契约一致 —— 不一致就等于在降级时制造一个永远不可用的契约。
    """

    #: 允许写字面量的降级兜底。改它必须同时改 contract 里那一份。
    # contract.py 本身就是唯一定义处；orders.py 是已声明的降级兜底。
    ALLOWED_LITERALS = {"contract.py", "orders.py"}

    def test_no_module_redefines_the_item_key_set(self):
        offenders = []
        for path in list(ROOT.glob("src/wechat_ai_assistant/*.py")) + \
                list(ROOT.glob("packages/order_agent/src/order_agent/*.py")):
            if path.name in self.ALLOWED_LITERALS:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("#", '"', "'", "*")) or "---" in stripped:
                    continue
                if all(t in stripped for t in ('"sku"', '"quantity"', '"unit"', '"evidence"')):
                    offenders.append(f"{path.name}: {stripped[:70]}")
        self.assertEqual(offenders, [],
                         f"\u8fd8\u6709\u5730\u65b9\u786c\u7f16\u7801\u4e86 item \u5b57\u6bb5\u6e05\u5355\uff1a{offenders}")


if __name__ == "__main__":
    unittest.main()
