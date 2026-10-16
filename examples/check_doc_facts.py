"""实测并核对文档里的数字。

## 为什么需要这个

文档里散落着"549 项测试""界面检查 29/29""打包 57 MB"这类具体数字。
它们全都**曾经正确**，然后悄悄过期了：

* 打包大小写 57 MB —— 实测 exe 本体 7.4 MB，整个目录 **85.4 MB**
  （1045 个文件）。商户问"占多大磁盘"，按 57 MB 答复是错的。
* 测试数和界面检查项数每次加断言就变，抄进文档必然对不上。

**过期数字比没有数字更糟**：读的人不会怀疑它。

所以这个脚本不去改文档，而是**实测后比对**，对不上就报错退出非零。
让它进收尾检查，文档就不可能再悄悄过期。
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 报告全是中文。默认控制台编码在 Windows 上是 cp1252，一打印就崩 ——
# 让脚本自带编码设置，不依赖谁用什么方式调用它。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def run_tests() -> tuple[int, int]:
    """跑全量单测，返回 (总数, 跳过数)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )
    text = proc.stdout + proc.stderr
    match = re.search(r"Ran (\d+) tests", text)
    total = int(match.group(1)) if match else 0
    skipped = len(re.findall(r"skipped=", text))
    return total, skipped


def run_ui_check() -> tuple[int, int]:
    """跑界面检查，返回 (通过数, 总数)。"""
    proc = subprocess.run(
        [sys.executable, "examples/merchant_ui_check.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )
    match = re.search(r"(\d+)/(\d+) 项通过", proc.stdout)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def measure_package() -> tuple[float, float, int]:
    """打包目录：(目录总 MB, exe MB, 文件数)。没打包就返回 (0, 0, 0)。"""
    dist = ROOT / "dist"
    if not dist.is_dir():
        return (0.0, 0.0, 0)
    best = (0.0, 0.0, 0)
    for folder in dist.iterdir():
        if not folder.is_dir():
            continue
        files = [f for f in folder.rglob("*") if f.is_file()]
        if not files:
            continue
        total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
        exes = [f for f in files if f.suffix.lower() == ".exe"]
        exe_mb = exes[0].stat().st_size / (1024 * 1024) if exes else 0.0
        best = (total_mb, exe_mb, len(files))
    return best


def doc_text(name: str) -> str:
    return (ROOT / "docs" / name).read_text(encoding="utf-8")


class StaleNumbersTests(unittest.TestCase):
    """文档里不该出现会过期的具体数字。"""

    def test_no_doc_claims_a_hard_coded_test_count(self):
        """像「Ran 549 tests」这种数字每次加断言就过期。"""
        pattern = re.compile(r"Ran\s+\d+\s+tests")
        offenders = []
        for path in (ROOT / "docs").glob("*.md"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}")
        self.assertFalse(offenders, "文档里写死了测试数量：" + "、".join(offenders))

    def test_no_doc_claims_a_hard_coded_ui_check_score(self):
        pattern = re.compile(r"\d+\s*/\s*\d+\s*项通过|界面检查\s*\*?\*?\d+\s*/\s*\d+")
        offenders = []
        for path in (ROOT / "docs").glob("*.md"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}  {line.strip()[:60]}")
        self.assertFalse(offenders, "文档里写死了界面检查项数：" + "；".join(offenders))

    def test_no_doc_claims_the_package_is_a_single_size(self):
        """exe 本体和整个目录不是一个数。写单个数字必然有一半是错的。"""
        pattern = re.compile(r"(独立程序|打包|exe)[^|\n]{0,20}\d+\s*MB")
        offenders = []
        for path in (ROOT / "docs").glob("*.md"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line) and "整个目录" not in line and "本体" not in line:
                    offenders.append(f"{path.name}:{number}  {line.strip()[:70]}")
        self.assertFalse(offenders, "文档把整个安装目录说成单个 exe 大小：" + "；".join(offenders))


class StaleBackendTests(unittest.TestCase):
    """默认后端已经换成 DeepSeek，写着 OpenCode 的部署说明是误导。"""

    def test_no_doc_says_opencode_is_required_to_run_the_product(self):
        """默认后端已经是 DeepSeek。产品不该再被描述成"必须先起 OpenCode"。

        刻意收窄：只认**"必须/需要 + OpenCode + 启动或运行"** 这类说法。
        早先写成"同��行里既有 opencode 又有必须就算"，结果把
        「只差**上游登录**：python examples/prepare_isolated_opencode.py」
        这类**验证流程**说明也判成违规 —— 那条说的是怎么验证真实模型，
        不是说产品离不开它。
        """
        misleading = re.compile(
            r"(必须|需要|得先|要先)[^\n]{0,20}(opencode|OpenCode)[^\n]{0,20}"
            r"(启动|运行|才能|才能跑|才能用)",
            re.IGNORECASE,
        )
        offenders = []
        for path in (ROOT / "docs").glob("*.md"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if misleading.search(line):
                    offenders.append(f"{path.name}:{number}  {line.strip()[:70]}")
        self.assertFalse(offenders, "文档仍说必须靠 OpenCode 才能跑：" + "；".join(offenders))

    def test_the_documents_say_deepseek_is_the_default_backend(self):
        """新的默认后端必须写进文档，否则部署的人会去配 OpenCode。"""
        found = False
        for path in (ROOT / "docs").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            if "DeepSeek" in text and "默认" in text:
                found = True
                break
        self.assertTrue(found, "文档里找不到「DeepSeek 是默认后端」的说明")

    def test_the_gateway_reads_the_deepseek_key_from_a_file(self):
        """密钥只从文件读。文档若还在教人用环境变量明文，是错的。"""
        source = (ROOT / "src" / "wechat_ai_assistant" / "gateway_server.py").read_text(encoding="utf-8")
        self.assertIn("WECHAT_AI_DEEPSEEK_API_KEY_FILE", source)
        self.assertNotIn("WECHAT_AI_DEEPSEEK_API_KEY", source.replace(
            "WECHAT_AI_DEEPSEEK_API_KEY_FILE", ""))


class FactsReport(unittest.TestCase):
    """把实测数字打出来。文档作者照着写，不要凭记忆。"""

    def test_report(self):
        total, _skipped = run_tests()
        passed, checks = run_ui_check()
        total_mb, exe_mb, files = measure_package()
        print()
        print("── 实测数字（写进文档前请以此为准）──")
        print(f"  单元测试        : {total} 项")
        print(f"  界面检查        : {passed}/{checks} 项")
        if files:
            print(f"  打包目录        : {total_mb:.1f} MB（{files} 个文件）")
            print(f"  其中 exe 本体   : {exe_mb:.1f} MB")
        else:
            print("  打包目录        : 本机没有 dist/，未测量")
        print("─────────────────────────────────────")


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(FactsReport)
    suite.addTests(loader.loadTestsFromTestCase(StaleNumbersTests))
    suite.addTests(loader.loadTestsFromTestCase(StaleBackendTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())