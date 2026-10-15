"""登录入口必须在第 1 步。

## 决定由来

商户的原话：**「登录放在第一步是不是更合理，第四步显示无模型」**。

两件事，第二件是直接证据：

1. 登录入口在第 4 步，商户一路走到"智能整理"才发现登不上。
2. 更糟的是那一屏写的是「**免费模型暂不可用**」—— 归错了因。真实原因是
   没登录、压根没去取过模型列表。商户会去反复点「刷新模型列表」，
   刷多少次都不会好，因为问题根本不在模型上。

我提过反对意见（前三步都不需要登录，登录失败会卡在原地），
商户的回复是"直接挪到第 1 步"。于是照办 —— 但**保留一条底线**：
登录**不挡路**。检查微信、选会话、读历史都不需要它，
读到的数据留在本地、不扣额度。

## 这组测试盯的就是这条底线

挪登录很容易顺手把它做成"必须登录才能继续"。
那会让登录失败的人只剩一个空界面。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class LoginIsStepOneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "src" / "pyweixin" / "merchant_ui.py").read_text(
            encoding="utf-8")

    def test_step_one_carries_the_login_entry(self):
        """登录入口只在顶栏：第 1 步不再摆产品账号卡（和顶栏重复）。"""
        start = self.source.index("def _page_wechat")
        end = self.source.index("def _refresh_wechat_recheck")
        block = self.source[start:end]
        self.assertNotIn("_build_login_block(frame)", block)
        self.assertIn("点此登录", self.source)

    def test_the_wechat_block_comes_before_the_login_block(self):
        """产品账号卡已整个拿掉：微信状态是第 1 步唯一的主角，
        登录只剩顶栏一个入口。"""
        start = self.source.index("def _page_wechat")
        end = self.source.index("def _refresh_wechat_recheck")
        block = self.source[start:end]
        self.assertNotIn("_build_login_block(frame)", block)
        self.assertNotIn('text="产品账号"', block)

    def test_it_says_login_is_required_to_proceed(self):
        """最要紧的一句。门禁规则下，不登录不许进入下一步，必须明说。
        
        反转记录：之前是"登录不挡路"，这里断言"没有登录也能用"。
        商户改成门禁制后，那句旧文案会变成假话，一起改掉。
        """
        start = self.source.index("def _render_login")
        block = self.source[start:start + 2500]
        self.assertIn("才能进入下一步", block)
        self.assertNotIn("没有登录也能用", block)

    def test_it_says_what_logging_in_is_actually_for(self):
        """登录不是万能钥匙，得说清只买哪一样能力。"""
        start = self.source.index("def _render_login")
        block = self.source[start:start + 2500]
        self.assertIn("智能整理", block)

    def test_step_one_is_titled_as_login_and_wechat(self):
        self.assertIn("第 1 步　登录并检查微信", self.source)

    def test_the_block_refreshes_after_login_and_after_logout(self):
        """登录完成后商户多半还停在第 1 步。框里写着"还没有登录"，
        会让他以为刚才那次登录没成功。"""
        self.assertIn("def _refresh_login_block", self.source)
        self.assertIn("self._refresh_login_block()", self.source)

    def test_logout_also_refreshes_it(self):
        start = self.source.index("def _on_logout_from_center")
        block = self.source[start:start + 1600]
        self.assertIn("_refresh_login_block()", block,
                      "退出登录后第 1 步那块也要变回去")


class WrongCauseTests(unittest.TestCase):
    """没登录不能说成"模型不可用"。

    这是商户实际撞上的那句：他在第 4 步看到「免费模型暂不可用」，
    于是去反复刷新模型列表 —— 而问题根本不在模型上。
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "src" / "pyweixin" / "application.py").read_text(
            encoding="utf-8")

    def test_not_logged_in_says_not_logged_in(self):
        start = self.source.index("def model_notice")
        block = self.source[start:start + 900]
        self.assertIn("还没有登录产品账号", block,
                      "未登录时必须说没登录，不能说模型不可用")

    def test_the_notice_checks_login_before_the_catalog(self):
        start = self.source.index("def model_notice")
        block = self.source[start:start + 900]
        self.assertLess(
            block.index("self.client is None"), block.index("free_available"),
            "必须先判登录，再判模型 —— 否则登录问题会被写成模型问题",
        )

    def test_the_model_refresh_button_is_disabled_when_logged_out(self):
        """模型选择模块已整个拿掉（模型由云端配置）：整理页没有模型按钮，
        也就没有"亮着但白点"的按钮。"""
        source = (ROOT / "src" / "pyweixin" / "merchant_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("def _refresh_model_controls", source)
        self.assertNotIn("def _build_model_picker", source)
        self.assertNotIn("刷新模型列表", source)


class NoFreeModelPromiseTests(unittest.TestCase):
    """产品里**没有免费模型**，界面不许这么说。

    商户的原话：**「产品没有免费模型和不扣额度的说法」**。

    我之前在第 1 步写了「读到的消息留在本机上，**不扣额度**」，在模型卡片上
    写了「**免费模型（推荐）**」「**免费**」。

    这两句都是**产品里不存在的东西**：
    * 初始分析额度为 0，没有免费模型 —— 说"免费"会让人以为不花钱
    * "不扣额度"暗示有一个额度正在被扣，读历史根本不走额度这条线

    商户照着这句话理解，提交后被扣额度，就会认为是软件骗人。
    """

    #: 会出现在商户屏幕上的文案所在文件。
    MERCHANT_VISIBLE = (
        "merchant_ui.py",
        "application.py",
        "client_login.py",
        "local_parse.py",
    )

    @classmethod
    def setUpClass(cls):
        cls.src = ROOT / "src" / "pyweixin"

    def _merchant_text(self, name: str) -> str:
        """只取**会显示给商户的字面量**。

        只看函数体里的字符串，**不看模块 docstring 和行注释** ——
        那些是给开发者看的，可以讨论"免费模型"这个概念；
        商户看不到，不该因此把机制相关说明也一起禁掉。
        """
        import io
        import tokenize

        source = (self.src / name).read_text(encoding="utf-8")
        out: list[str] = []
        prev = ""
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.STRING:
                # 模块/类/函数开头的 docstring 是给人看的，跳过。
                at_start = prev.strip() in ("", ":") or prev.rstrip().endswith(
                    ('"""', "'''"))
                if not at_start:
                    out.append(tok.string)
            elif tok.type in (tokenize.FSTRING_START, tokenize.FSTRING_MIDDLE):
                out.append(tok.string)
            if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                tokenize.INDENT, tokenize.DEDENT):
                prev = tok.string
        return "\n".join(out)

    def test_no_merchant_screen_says_免费模型(self):
        for name in self.MERCHANT_VISIBLE:
            with self.subTest(file=name):
                self.assertNotIn("免费模型", self._merchant_text(name),
                                 f"{name} 里有商户能看到的「免费模型」")

    def test_no_merchant_screen_promises_不扣额度(self):
        for name in self.MERCHANT_VISIBLE:
            with self.subTest(file=name):
                self.assertNotIn("不扣额度", self._merchant_text(name),
                                 f"{name} 里有商户能看到的「不扣额度」")

    def test_the_model_card_says_what_actually_happens(self):
        """模型选择模块已整个拿掉（模型由云端配置）：界面上没有模型卡片，
        也就没有"推荐/免费"一类的价格承诺可写错。"""
        text = self._merchant_text("merchant_ui.py")
        self.assertNotIn("推荐模型", text)
        self.assertNotIn("选择分析模型", text)
        self.assertNotIn("刷新模型列表", text)

    def test_the_login_block_explains_local_reading_without_the_额度_wording(self):
        """登录框只说门禁（不登录不让进）并指向右上角唯一入口，不牵扯额度。"""
        text = self._merchant_text("merchant_ui.py")
        self.assertIn("需要先登录产品账号，才能进入下一步选会话", text)
        self.assertNotIn("会使用分析额度", text)

    def test_the_backend_may_keep_the_mechanism(self):
        """网关侧的免费额度机制是**运营可配的能力**，不该因为界面不显示
        就把它删掉 —— 那会误伤将来开启免费模型的能力。

        这里明确记录边界：机制留着，界面不许承诺。
        """
        gateway = (self.src / "gateway.py").read_text(encoding="utf-8")
        self.assertIn("no_free_model", gateway,
                      "网关的错误码是接口约定，不该被界面改动牵连")


if __name__ == "__main__":
    unittest.main()