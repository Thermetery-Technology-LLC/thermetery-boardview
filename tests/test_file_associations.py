"""Keep OS open-with declarations in sync with the parser registry."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from src.parsers.boardview import ALL_EXTS

ROOT = Path(__file__).resolve().parents[1]


def test_windows_associations_cover_registered_formats():
    script = (ROOT / "packaging/installer.iss").read_text(encoding="utf-8")
    defaults = set(re.findall(
        r'Subkey: "Software\\Classes\\(\.[a-z0-9]+)";.*?'
        r"Check: CanAssociate\('(\.[a-z0-9]+)'\)", script))
    assert defaults == {(ext, ext) for ext in ALL_EXTS}
    open_with = set(re.findall(
        r'Subkey: "Software\\Classes\\(\.[a-z0-9]+)\\OpenWithProgids"', script))
    assert open_with == ALL_EXTS
    assert '"""{app}\\ThermeteryBoardviewer.exe"" ""%1"""' in script


def test_android_view_filter_covers_registered_formats():
    root = ET.parse(ROOT / "android/app/src/main/AndroidManifest.xml").getroot()
    ns = "{http://schemas.android.com/apk/res/android}"
    filters = root.findall(".//activity/intent-filter")
    view = [f for f in filters if any(
        a.get(ns + "name") == "android.intent.action.VIEW"
        for a in f.findall("action"))]
    patterns = {d.get(ns + "pathPattern", "") for f in view for d in f.findall("data")}
    for ext in ALL_EXTS:
        assert any(pattern.endswith("\\\\" + ext) for pattern in patterns), ext
    assert any(d.get(ns + "mimeType") == "application/octet-stream"
               for f in view for d in f.findall("data"))
