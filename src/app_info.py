# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Project identity for About screens: name, version, copyright, licence
and links. Stdlib-only (imported by the desktop viewer; safe under
Chaquopy too).

The renderer (android/app/src/main/assets/viewer.js), the Android shell
(AppInfo.kt), the iOS shell and the build files carry their own copies;
tests/test_app_info.py fails when any of them drift from this module.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import quote

APP_NAME = "Thermetery Boardview"
# Release version. Must match meson.build (the Windows release build reads
# it there), android versionName and the iOS MARKETING_VERSION.
__version__ = "1.2.0"
COPYRIGHT = "Copyright © 2026 Thermetery Technology LLC"
LICENSE_NAME = "GNU Lesser General Public License v3.0 or later"
LICENSE_SPDX = "LGPL-3.0-or-later"
LICENSE_NOTICE = (
    "This program is free software: you can redistribute it and/or modify "
    "it under the terms of the GNU Lesser General Public License as "
    "published by the Free Software Foundation, either version 3 of the "
    "License, or (at your option) any later version. It is distributed "
    "WITHOUT ANY WARRANTY; without even the implied warranty of "
    "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE."
)
REPO_URL = "https://github.com/Thermetery-Technology-LLC/thermetery-boardview"
ISSUES_NEW_URL = REPO_URL + "/issues/new"
LICENSE_URL = REPO_URL + "/blob/main/LICENSE"


def _git_commit() -> Optional[str]:
    """Short commit hash when running from a git checkout, else None."""
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


def build_description() -> str:
    """'1.2.0 (Windows build)' / '1.2.0 (source, abc1234)'."""
    if getattr(sys, "frozen", False):
        kind = f"{platform.system() or 'desktop'} build"
    else:
        commit = _git_commit()
        kind = f"source, {commit}" if commit else "source"
    return f"{__version__} ({kind})"


def os_description() -> str:
    return (f"{platform.system()} {platform.release()} "
            f"({platform.machine()}), Python {platform.python_version()}")


def issue_url(board_format: Optional[str] = None) -> str:
    """GitHub new-issue page pre-filled with what a report needs."""
    lines = [
        f"**App:** {APP_NAME} {build_description()} · desktop",
        f"**OS:** {os_description()}",
    ]
    if board_format:
        lines.append(f"**Board format:** {board_format}")
    lines += ["", "**What happened:**", "", "",
              "**What you expected:**", "", "",
              "**Steps / screenshots:**", ""]
    return ISSUES_NEW_URL + "?body=" + quote("\n".join(lines), safe="")


__all__ = [
    "APP_NAME", "COPYRIGHT", "ISSUES_NEW_URL", "LICENSE_NAME",
    "LICENSE_NOTICE", "LICENSE_SPDX", "LICENSE_URL", "REPO_URL",
    "__version__", "build_description", "issue_url", "os_description",
]
