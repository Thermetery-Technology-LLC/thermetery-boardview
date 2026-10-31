"""Windows frozen-entry wrapper for Thermetery Boardviewer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from multiprocessing import freeze_support
import sys
import tempfile
import traceback


APP_USER_MODEL_ID = "ThermeteryTechnology.ThermeteryBoardviewer"
SELF_TEST_FLAG = "--packaging-self-test"
SELF_TEST_OUTPUT_ENV = "BOARDVIEWER_SELF_TEST_OUTPUT"
SELF_TEST_HANG_SECONDS = 45  # under build_windows.ps1's 60 s self-test timeout
_LOG_STREAM = None


def _set_windows_app_id() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except (AttributeError, OSError):
        pass


def _prepare_stdio() -> Path:
    """Give a windowed build a durable diagnostic stream."""
    global _LOG_STREAM
    from src.runtime_paths import managed_data_dir

    data_dir = managed_data_dir()
    if data_dir is None:
        data_dir = Path.cwd()
    log_path = data_dir / "boardviewer.log"
    if sys.stdout is not None and sys.stderr is not None:
        return log_path

    candidates = (
        log_path,
        Path(tempfile.gettempdir()) / "Thermetery Boardviewer" / "boardviewer.log",
    )
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            _LOG_STREAM = candidate.open("a", encoding="utf-8", buffering=1)
            log_path = candidate
            break
        except OSError:
            continue
    else:
        _LOG_STREAM = open(os.devnull, "w", encoding="utf-8")
        log_path = Path(os.devnull)

    if sys.stdout is None:
        sys.stdout = _LOG_STREAM
    if sys.stderr is None:
        sys.stderr = _LOG_STREAM
    return log_path


def _write_self_test_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _run_packaging_self_test(output: Path) -> int:
    """Exercise imports, Tcl/Tk data, writable storage, and every native DLL."""
    import faulthandler

    from src.runtime_paths import managed_data_dir, portable_mode, private_dir

    result = {
        "ok": False,
        "portable": portable_mode(),
        "data_dir": str(managed_data_dir()),
        "private_dir": str(private_dir()),
        "checks": {},
    }
    # If a check wedges (a Tcl/Tk init that never returns, a modal error
    # dialog raised by a system DLL) the build script only sees its 60 s
    # timeout. Dump every thread's stack into the log stream first, then
    # exit, so the log names the hanging call.
    hang_log = sys.stderr if sys.stderr is not None else _LOG_STREAM
    try:
        faulthandler.dump_traceback_later(
            SELF_TEST_HANG_SECONDS, repeat=False, file=hang_log, exit=True
        )
    except (AttributeError, OSError, ValueError):
        pass
    try:
        print("self-test: importing numpy", flush=True)
        import numpy

        result["checks"]["numpy"] = numpy.__version__

        print("self-test: importing skia", flush=True)
        import skia

        skia.Surface(2, 2)
        result["checks"]["skia"] = getattr(skia, "__version__", "imported")

        print("self-test: importing PyOpenGL / pyopengltk", flush=True)
        from OpenGL import GL  # noqa: F401
        import pyopengltk.win32  # noqa: F401

        result["checks"]["opengl"] = True

        from tkinterdnd2 import TkinterDnD

        print("self-test: creating Tk root", flush=True)
        root = TkinterDnD.Tk()
        result["checks"]["tcl_tk"] = str(root.tk.call("info", "patchlevel"))
        # TkinterDnD.Tk() already loaded tkdnd; record which build answered
        # so the log proves the Tcl-9 (or 8.6) binary matching this
        # runtime was collected, not merely that the import succeeded.
        result["checks"]["tkdnd"] = str(root.tk.call("package", "present", "tkdnd"))
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        result["checks"]["tkinterdnd2"] = True

        from src.parsers.tvw_native import _load as load_tvw
        from src.parsers.xzz_native import available as xzz_available
        from src.parsers.fz_parser import _load_native_rc6

        native = {
            "tvw_native": load_tvw() is not None,
            "xzz_native": xzz_available(),
            "rc6_native": _load_native_rc6() is not None,
        }
        if not all(native.values()):
            missing = ", ".join(name for name, present in native.items() if not present)
            raise RuntimeError(f"native library check failed: {missing}")
        result["checks"].update(native)

        print("self-test: importing src.viewer", flush=True)
        from src import viewer  # noqa: F401

        result["checks"]["viewer"] = True

        data_dir = managed_data_dir()
        if data_dir is None:
            raise RuntimeError("frozen self-test has no managed data directory")
        probe = data_dir / ".write-test"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        result["checks"]["writable_data"] = True
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    finally:
        faulthandler.cancel_dump_traceback_later()

    _write_self_test_result(output, result)
    return 0 if result["ok"] else 1


def _show_fatal_error(log_path: Path, exc: Exception) -> None:
    traceback.print_exc()
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Thermetery Boardviewer",
            f"The application could not start.\n\n{exc}\n\n"
            f"Diagnostics were written to:\n{log_path}",
            parent=root,
        )
        root.destroy()
    except Exception:
        pass


def main() -> int:
    freeze_support()
    _set_windows_app_id()
    log_path = _prepare_stdio()

    if SELF_TEST_FLAG in sys.argv[1:]:
        output = os.environ.get(SELF_TEST_OUTPUT_ENV, "").strip()
        if not output:
            print(f"{SELF_TEST_OUTPUT_ENV} is required for {SELF_TEST_FLAG}")
            return 2
        return _run_packaging_self_test(Path(output))

    try:
        from src.viewer import main as viewer_main

        viewer_main()
    except Exception as exc:
        _show_fatal_error(log_path, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
