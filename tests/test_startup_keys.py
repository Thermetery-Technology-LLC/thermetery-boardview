from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from src import viewer


class StartupKeyTests(unittest.TestCase):
    def test_encrypted_fz_prompts_and_returns_retried_board(self):
        path = Path("first.fz")
        error = viewer.FZKeyError("missing key")
        board = object()
        with mock.patch.object(viewer, "parse_board", side_effect=error), \
             mock.patch.object(viewer, "_prompt_for_startup_key", return_value=board) as prompt:
            self.assertIs(viewer._load_startup_board(path), board)
        prompt.assert_called_once_with(path, fmt="fz", initial_error=error)

    def test_cancelling_fz_returns_none(self):
        with mock.patch.object(viewer, "parse_board", side_effect=viewer.FZKeyError("missing")), \
             mock.patch.object(viewer, "_prompt_for_startup_key", return_value=None):
            self.assertIsNone(viewer._load_startup_board(Path("first.fz")))

    def test_xzz_prompts_and_cancel_keeps_partial_board(self):
        board = SimpleNamespace(key_required=True)
        path = Path("first.pcb")
        with mock.patch.object(viewer, "parse_board", return_value=board), \
             mock.patch.object(viewer, "_prompt_for_startup_key", return_value=None) as prompt:
            self.assertIs(viewer._load_startup_board(path), board)
        prompt.assert_called_once_with(path, fmt="xzz")

    def test_smoke_test_does_not_open_dialog(self):
        with mock.patch.object(viewer, "parse_board", side_effect=viewer.FZKeyError("missing")), \
             mock.patch.object(viewer, "_prompt_for_startup_key") as prompt:
            with self.assertRaises(viewer.FZKeyError):
                viewer._load_startup_board(Path("first.fz"), smoke_test=True)
        prompt.assert_not_called()

    def test_startup_dialog_retries_and_saves_working_key(self):
        board = object()
        owner = SimpleNamespace(_maybe_save_key=mock.Mock())
        with mock.patch.object(viewer.simpledialog, "askstring", side_effect=["bad", "good"]), \
             mock.patch.object(viewer, "parse_board", side_effect=[viewer.FZKeyError("bad"), board]):
            result = viewer.ViewerApp._load_with_key_prompt(owner, Path("first.fz"), fmt="fz")
        self.assertIs(result, board)
        owner._maybe_save_key.assert_called_once_with("fz", "good")

    def test_dialog_root_is_destroyed_on_cancel(self):
        with mock.patch.object(viewer, "_StartupKeyPrompt") as root_type:
            root = root_type.return_value
            root._load_with_key_prompt.return_value = None
            self.assertIsNone(viewer._prompt_for_startup_key(Path("first.fz"), fmt="fz"))
        root.withdraw.assert_called_once()
        root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
