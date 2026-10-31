"""Desktop view mirroring: the canvas API and the viewer's Mirror widgets.

CanvasCommon is driven through a tk-free stub (only the orientation state
and `bounds` are needed), and the viewer's handlers are called on a small
fake `self`, so none of this needs a display and it runs on every CI
runner. The real-widget behaviour (toolbar button, View menu check, X key)
is covered by the manual probe documented in the PR.
"""

import itertools
import unittest

from src.board_canvas import CanvasCommon
from src.viewer import ViewerApp

LAYERS = ("TOP", "BOTTOM", "INNER_1")


class _MirrorCanvas(CanvasCommon):
    """CanvasCommon with just the orientation state; `_reorient` runs the
    mutation directly and counts calls."""

    def __init__(self, layer: str = "TOP"):
        self._view_layer = layer
        self._mirror_x = False
        self._rotation_quadrant = 0
        self.bounds = (0.0, 0.0, 100.0, 50.0)
        self.reorients = 0

    def _reorient(self, mutate):
        self.reorients += 1
        mutate()


class MirrorStateTests(unittest.TestCase):
    def test_is_mirrored_truth_table(self):
        # BOTTOM mirrors on its own; the user toggle XORs with that.
        for layer, user in itertools.product(LAYERS, (False, True)):
            cv = _MirrorCanvas(layer)
            cv._mirror_x = user
            self.assertEqual(cv.is_mirrored, (layer == "BOTTOM") != user,
                             f"{layer} user={user}")
            self.assertEqual(cv.mirror_x, user)

    def test_projection_flips_x_exactly_when_is_mirrored(self):
        for layer, user in itertools.product(LAYERS, (False, True)):
            cv = _MirrorCanvas(layer)
            cv._mirror_x = user
            x, y = cv._apply_view_transform(10.0, 5.0)
            self.assertAlmostEqual(x, 90.0 if cv.is_mirrored else 10.0)
            self.assertAlmostEqual(y, 5.0)

    def test_two_parts_swap_sides_on_bottom_like_android(self):
        # The C976 / C975 pair the Android check uses: left/right order
        # reverses on BOTTOM, and BOTTOM + Mirror restores it.
        def left_to_right(layer, user):
            cv = _MirrorCanvas(layer)
            cv._mirror_x = user
            xa = cv._apply_view_transform(30.0, 20.0)[0]   # "C976"
            xb = cv._apply_view_transform(60.0, 20.0)[0]   # "C975"
            return "C976 first" if xa < xb else "C975 first"

        self.assertEqual(left_to_right("TOP", False), "C976 first")
        self.assertEqual(left_to_right("BOTTOM", False), "C975 first")
        self.assertEqual(left_to_right("BOTTOM", True), "C976 first")
        self.assertEqual(left_to_right("TOP", True), "C975 first")

    def test_view_transform_round_trips_for_every_orientation(self):
        for layer, user, quad in itertools.product(LAYERS, (False, True),
                                                   range(4)):
            cv = _MirrorCanvas(layer)
            cv._mirror_x = user
            cv._rotation_quadrant = quad
            for pt in ((10.0, 5.0), (0.0, 0.0), (100.0, 50.0), (37.5, 41.0)):
                back = cv._invert_view_transform(*cv._apply_view_transform(*pt))
                self.assertAlmostEqual(back[0], pt[0], msg=(layer, user, quad))
                self.assertAlmostEqual(back[1], pt[1], msg=(layer, user, quad))

    def test_set_mirror_is_idempotent_and_toggle_reverses(self):
        cv = _MirrorCanvas("TOP")
        cv.set_mirror_x(False)
        self.assertEqual(cv.reorients, 0, "no change -> no re-orient")
        cv.set_mirror_x(True)
        self.assertTrue(cv.mirror_x)
        self.assertEqual(cv.reorients, 1)
        cv.set_mirror_x(True)
        self.assertEqual(cv.reorients, 1)
        cv.toggle_mirror_x()
        self.assertFalse(cv.mirror_x)
        cv.toggle_mirror_x()
        self.assertTrue(cv.mirror_x)
        self.assertEqual(cv.reorients, 3)

    def test_layer_indicator_says_whether_the_view_is_mirrored(self):
        expect = {
            ("TOP", False): "TOP (looking down)",
            ("TOP", True): "TOP (mirrored)",
            ("BOTTOM", False): "BOTTOM (mirrored, as if board flipped)",
            ("BOTTOM", True): "BOTTOM (not mirrored)",
        }
        for (layer, user), text in expect.items():
            cv = _MirrorCanvas(layer)
            cv._mirror_x = user
            self.assertEqual(cv._layer_indicator(), text)


class _Btn:
    def __init__(self):
        self.text = None

    def config(self, **kw):
        self.text = kw.get("text", self.text)


class _Var:
    def __init__(self):
        self.value = None

    def set(self, v):
        self.value = v


class _FakeApp:
    """Just enough of ViewerApp for the Mirror handlers."""

    _toggle_mirror = ViewerApp._toggle_mirror
    _sync_mirror_widgets = ViewerApp._sync_mirror_widgets
    _sync_layer_widgets = ViewerApp._sync_layer_widgets

    def __init__(self, canvas):
        self.canvas = canvas
        self.mirror_btn = _Btn()
        self._mirror_var = _Var()
        self.status_updates = 0

    def _update_status(self):
        self.status_updates += 1


class ViewerMirrorWidgetTests(unittest.TestCase):
    def test_toggle_updates_button_menu_check_and_status(self):
        app = _FakeApp(_MirrorCanvas("TOP"))
        app._sync_mirror_widgets()
        self.assertEqual(app.mirror_btn.text, "Mirror: OFF")
        self.assertIs(app._mirror_var.value, False)
        app._toggle_mirror()
        self.assertEqual(app.mirror_btn.text, "Mirror: ON")
        self.assertIs(app._mirror_var.value, True)
        self.assertEqual(app.status_updates, 1)
        app._toggle_mirror()
        self.assertEqual(app.mirror_btn.text, "Mirror: OFF")
        self.assertIs(app._mirror_var.value, False)

    def test_button_reads_on_for_bottom_and_off_after_the_user_flips_it(self):
        app = _FakeApp(_MirrorCanvas("BOTTOM"))
        app._sync_mirror_widgets()
        self.assertEqual(app.mirror_btn.text, "Mirror: ON")    # automatic flip
        app._toggle_mirror()
        self.assertEqual(app.mirror_btn.text, "Mirror: OFF")   # un-mirrored
        self.assertTrue(app.canvas.mirror_x)

    def test_layer_change_refreshes_the_mirror_widgets(self):
        app = _FakeApp(_MirrorCanvas("TOP"))
        app._toggle_mirror()                       # user mirror on, TOP
        self.assertEqual(app.mirror_btn.text, "Mirror: ON")
        app.canvas._view_layer = "BOTTOM"          # as set_view_layer does
        app._sync_layer_widgets("BOTTOM")          # the layer-change path
        self.assertEqual(app.mirror_btn.text, "Mirror: OFF")  # ON xor BOTTOM
        app.canvas._view_layer = "TOP"
        app._sync_layer_widgets("TOP")
        self.assertEqual(app.mirror_btn.text, "Mirror: ON")

    def test_sync_before_the_toolbar_exists_is_harmless(self):
        app = _FakeApp(_MirrorCanvas("TOP"))
        del app.mirror_btn
        del app._mirror_var
        app._sync_mirror_widgets()                 # must not raise


if __name__ == "__main__":
    unittest.main()
