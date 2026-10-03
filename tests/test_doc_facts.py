"""文档里的数字必须是真的 —— 或者干脆不写死。

`examples/check_doc_facts.py` 是权威检查（它会真跑一遍拿到实测数字）。
这里只做**静态**那部分，让全量单测每次都覆盖到，不必单独跑脚本。

## 为什么较真

文档里散着"549 项测试""界面检查 29/29""打包 57 MB"。它们全都**曾经正确**，
然后悄悄过期了。打包那个尤其要命：exe 本体 7.4 MB，整个目录 85.4 MB，
商户问"占多大磁盘"，按 57 MB 答复是错的。

**过期数字比没有数字更糟** —— 读的人不会怀疑它。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DOCS = ROOT / "docs"


def doc_lines():
    for path in sorted(DOCS.glob("*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield path.name, number, line


class NoStaleCountsTests(unittest.TestCase):
    def test_no_hard_coded_test_counts(self):
        pattern = re.compile(r"Ran\s+\d+\s+tests")
        bad = [f"{name}:{n}" for name, n, line in doc_lines() if pattern.search(line)]
        self.assertFalse(bad, "文档写死了测试数量：" + "、".join(bad))

    def test_no_hard_coded_ui_check_scores(self):
        pattern = re.compile(r"\d+\s*/\s*\d+\s*项通过")
        bad = [f"{name}:{n} {line.strip()[:50]}"
               for name, n, line in doc_lines() if pattern.search(line)]
        self.assertFalse(bad, "文档写死了界面检查项数：" + "；".join(bad))

    def test_package_size_is_not_reported_as_a_single_number(self):
        pattern = re.compile(r"(独立程序|打包|exe)[^|\n]{0,20}\d+\s*MB")
        bad = [f"{name}:{n} {line.strip()[:60]}"
               for name, n, line in doc_lines()
               if pattern.search(line) and "整个目录" not in line and "本体" not in line]
        self.assertFalse(bad, "把整个安装目录说成单个 exe 大小：" + "；".join(bad))


class BackendFactsTests(unittest.TestCase):
    def test_deepseek_is_documented_as_the_default_backend(self):
        text = (DOCS / "model-catalog.md").read_text(encoding="utf-8")
        self.assertIn("默认后端是 DeepSeek", text,
                      "部署的人找不到该配什么，就会去配 OpenCode")

    def test_the_key_is_documented_as_file_only(self):
        text = (DOCS / "model-catalog.md").read_text(encoding="utf-8")
        self.assertIn("PYWEIXIN_DEEPSEEK_API_KEY_FILE", text)
        self.assertIn("只从文件读", text,
                      "必须写明：环境变量明文会进部署日志")

    def test_opencode_is_not_described_as_required(self):
        misleading = re.compile(
            r"(必须|需要|得先|要先)[^\n]{0,20}(opencode|OpenCode)[^\n]{0,20}"
            r"(启动|运行|才能|才能跑|才能用)",
            re.IGNORECASE,
        )
        bad = [f"{name}:{n} {line.strip()[:60]}"
               for name, n, line in doc_lines() if misleading.search(line)]
        self.assertFalse(bad, "仍说必须靠 OpenCode 才能跑：" + "；".join(bad))

    def test_the_facts_script_exists_and_is_runnable(self):
        script = ROOT / "examples" / "check_doc_facts.py"
        self.assertTrue(script.is_file(), "实测脚本不见了，文档数字又会开始过期")
        self.assertIn("def run_tests", script.read_text(encoding="utf-8"),
                      "实测脚本必须真的跑一遍，不能只打印缓存数字")


class MerchantFacingClaimsTests(unittest.TestCase):
    """文档里给商户看的说法也要如实。"""

    def test_no_doc_claims_receipts_are_verified_on_real_wechat(self):
        """真实发送还没在真机验证过，文档不能写成已经验证。"""
        bad = []
        for name, n, line in doc_lines():
            if re.search(r"(真实)?发送[^。]{0,10}(已|已经)(在)?真机验证", line):
                bad.append(f"{name}:{n}")
        self.assertFalse(bad, "文档说真实发送已在真机验证：" + "、".join(bad))

    def test_the_free_quota_policy_is_not_described_as_on_by_default(self):
        """默认不提供每日免费额度（新账号余额为 0）。"""
        for name, n, line in doc_lines():
            if "daily_free_limit" in line:
                self.assertNotIn("daily_free_limit=1000", line,
                                 f"{name}:{n} 把默认免费额度写成 1000")


if __name__ == "__main__":
    unittest.main()