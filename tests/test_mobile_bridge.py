"""The window.Android bridge stays in sync across the contract, the
renderer and both shells (docs/android_contract.md §3/§4), and the Open
recent limits match between the shells and the desktop viewer.

These are static checks: the shells cannot be built in CI (Android needs
the SDK + Chaquopy, iOS needs Xcode), so drift between the four copies of
the bridge would otherwise only show up on a device.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "android" / "app" / "src" / "main" / "assets"
KT_DIR = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "thermetery" / "boardview"
SWIFT_DIR = ROOT / "ios" / "Boardview" / "Sources"

RECENT_METHODS = {"listRecent", "openRecent", "removeRecent", "clearRecent"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:nxt if nxt >= 0 else len(text)]


def contract_methods() -> set:
    sec = _section(_read(ROOT / "docs" / "android_contract.md"), "## 4.")
    return set(re.findall(r"^Android\.(\w+)\(", sec, re.M))


def kotlin_bridge_methods() -> set:
    kt = _read(KT_DIR / "BoardviewBridge.kt")
    return set(re.findall(r"@JavascriptInterface\s+fun\s+(\w+)\s*\(", kt))


def swift_shim_methods() -> set:
    swift = _read(SWIFT_DIR / "ViewController.swift")
    m = re.search(r"window\.Android = \{(.*?)\n\s*\};", swift, re.S)
    assert m, "window.Android shim not found in ViewController.swift"
    return set(re.findall(r"^\s*(\w+): function", m.group(1), re.M))


def swift_handled_commands() -> set:
    swift = _read(SWIFT_DIR / "ViewController.swift")
    m = re.search(r"func userContentController\(.*?\n    \}\n", swift, re.S)
    assert m, "userContentController not found"
    return set(re.findall(r'case "(\w+)"', m.group(0)))


class BridgeParityTest(unittest.TestCase):
    def test_contract_lists_recent_methods(self):
        self.assertTrue(RECENT_METHODS <= contract_methods())

    def test_kotlin_bridge_matches_contract(self):
        self.assertEqual(kotlin_bridge_methods(), contract_methods())

    def test_ios_shim_matches_contract(self):
        self.assertEqual(swift_shim_methods(), contract_methods())

    def test_ios_handles_every_async_method(self):
        # appInfo() is answered inside the shim (a document-start global).
        missing = contract_methods() - {"appInfo"} - swift_handled_commands()
        self.assertFalse(missing, f"no userContentController case for {sorted(missing)}")

    def test_renderer_only_calls_contract_methods(self):
        js = _read(ASSETS / "viewer.js")
        used = set(re.findall(r"window\.Android\.(\w+)", js))
        self.assertFalse(used - contract_methods(),
                         f"viewer.js calls methods missing from §4: {sorted(used - contract_methods())}")
        self.assertTrue(RECENT_METHODS <= used)

    def test_on_recent_wired_everywhere(self):
        contract = _section(_read(ROOT / "docs" / "android_contract.md"), "## 3.")
        self.assertIn("bv.onRecent(", contract)
        self.assertIn("window.bv.onRecent = function", _read(ASSETS / "viewer.js"))
        self.assertIn("bv.onRecent(", _read(KT_DIR / "MainActivity.kt"))
        self.assertIn("bv.onRecent(", _read(SWIFT_DIR / "ViewController.swift"))

    def test_renderer_hides_recent_without_a_shell(self):
        js = _read(ASSETS / "viewer.js")
        self.assertRegex(js, r"var hasRecent = hasAndroid &&")
        html = _read(ASSETS / "viewer.html")
        # Hidden by default; viewer.js reveals it only when hasRecent.
        self.assertRegex(html, r'<button id="bv-btn-recent" class="hidden"')
        self.assertRegex(html, r'<div id="bv-home" class="hidden"')
        self.assertRegex(html, r'<div id="bv-recent" class="hidden"')


class RecentLimitsTest(unittest.TestCase):
    def test_limit_matches_desktop(self):
        desktop = int(re.search(r"^_RECENT_LIMIT = (\d+)", _read(ROOT / "src" / "viewer.py"),
                                re.M).group(1))
        kt = int(re.search(r"const val LIMIT = (\d+)", _read(KT_DIR / "RecentStore.kt")).group(1))
        swift = int(re.search(r"static let limit = (\d+)",
                              _read(SWIFT_DIR / "RecentStore.swift")).group(1))
        self.assertEqual((kt, swift), (desktop, desktop))

    def test_copy_budget_matches_between_shells(self):
        kt = re.search(r"COPY_BUDGET_BYTES = (\d+)L \* 1024 \* 1024",
                       _read(KT_DIR / "RecentStore.kt")).group(1)
        swift = re.search(r"copyBudgetBytes: Int64 = (\d+) \* 1024 \* 1024",
                          _read(SWIFT_DIR / "RecentStore.swift")).group(1)
        self.assertEqual(kt, swift)
        self.assertIn(f"{kt} MB", _read(ROOT / "privacy.md"))


if __name__ == "__main__":
    unittest.main()
