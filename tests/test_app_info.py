"""About-page identity stays in sync across desktop, renderer and shells."""
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src import app_info

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "android" / "app" / "src" / "main" / "assets"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class VersionSyncTest(unittest.TestCase):
    def test_meson_version(self):
        m = re.search(r"version\s*:\s*'([^']+)'", _read("meson.build"))
        self.assertEqual(m.group(1), app_info.__version__)

    def test_android_version_name(self):
        m = re.search(r'versionName\s*=\s*"([^"]+)"',
                      _read("android/app/build.gradle.kts"))
        self.assertEqual(m.group(1), app_info.__version__)

    def test_ios_marketing_version(self):
        m = re.search(r'MARKETING_VERSION:\s*"([^"]+)"', _read("ios/project.yml"))
        self.assertEqual(m.group(1), app_info.__version__)


class IdentitySyncTest(unittest.TestCase):
    def test_renderer_constants(self):
        js = (ASSETS / "viewer.js").read_text(encoding="utf-8")
        self.assertIn(f'var REPO_URL = "{app_info.REPO_URL}";', js)
        self.assertIn(f'var APP_NAME = "{app_info.APP_NAME}";', js)
        self.assertIn('ISSUES_NEW_URL = REPO_URL + "/issues/new"', js)
        self.assertIn('LICENSE_URL = REPO_URL + "/blob/main/LICENSE"', js)
        year_holder = re.search(r"Copyright (?:\\u00A9|©) (\d{4}) ([^\"]+)\"", js)
        self.assertIsNotNone(year_holder)
        self.assertIn(f"{year_holder.group(1)} {year_holder.group(2)}",
                      app_info.COPYRIGHT)

    def test_shell_repo_urls(self):
        kt = _read("android/app/src/main/java/com/thermetery/boardview/AppInfo.kt")
        self.assertIn(f'REPO_URL = "{app_info.REPO_URL}"', kt)
        swift = _read("ios/Boardview/Sources/ViewController.swift")
        self.assertIn(f'repoURL = "{app_info.REPO_URL}"', swift)

    def test_license_file_is_lgpl3(self):
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", _read("LICENSE"))
        self.assertIn("Version 3", _read("LICENSE"))


class IssueUrlTest(unittest.TestCase):
    def test_prefilled_body(self):
        url = app_info.issue_url("fz")
        parsed = urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         app_info.ISSUES_NEW_URL)
        body = parse_qs(parsed.query)["body"][0]
        self.assertIn(app_info.__version__, body)
        self.assertIn("**Board format:** fz", body)
        self.assertIn("**What happened:**", body)

    def test_no_format_line_without_board(self):
        body = parse_qs(urlparse(app_info.issue_url()).query)["body"][0]
        self.assertNotIn("Board format", body)

    def test_build_description_has_version(self):
        self.assertTrue(app_info.build_description().startswith(
            app_info.__version__ + " ("))


if __name__ == "__main__":
    unittest.main()
