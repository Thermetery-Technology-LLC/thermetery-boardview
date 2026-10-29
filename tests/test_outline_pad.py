# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Coverage for the shared component-outline pad rule (src/units.py), its
two consumers (board_export._component_outline and
CanvasCommon._component_polygon_world), the near-miss component pick,
the all-pins overlay (gate, hit-test, via arbitration), the NetInfoPanel
row-focus path and the startup launcher's row model and key handling.

The canvas tests drive CanvasCommon through a tk-free stub (projection
is the identity, the widget is 1000x1000) so they stay headless-CI safe.
The tests that need real tk widgets skip when no display is available."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import board_export
from src import units
from src.board_canvas import CanvasCommon
from src.parsers.gencad_parser import BoardModel, Component, Shape


# TVW 0402 as the parser emits it: two pins on the y axis, zero width,
# bbox_override equal to the pin extent. Centi-mil units (3937 / mm).
def _tvw_r0402() -> Shape:
    pins = [("1", 0.0, 1591.0), ("2", 0.0, -1591.0)]
    return Shape(name="_tvw_R0402_1", pins=pins,
                 bbox_override=(0.0, -1591.0, 0.0, 1591.0))


# TVW PCIe finger as the parser emits it: every pad listed twice (A1 and
# 1 at one position), one pair a single unit apart, 1 mm real pitch.
def _tvw_finger(n: int = 20) -> Shape:
    pins = []
    for i in range(n):
        y = -40000.0 + i * 3937.0
        pins.append((f"A{i + 1}", 0.0, y))
        pins.append((str(i + 1), 0.0, y + (1.0 if i == 3 else 0.0)))
    return Shape(name="_tvw_CON_FINGER", pins=pins)


CMIL = units.CENTIMILS_PER_MM


class OutlinePadHelperTests(unittest.TestCase):
    def test_pin_pitch(self):
        self.assertIsNone(units.pin_pitch([]))
        self.assertIsNone(units.pin_pitch([("1", 0, 0)]))
        # Coincident pins carry no pitch information.
        self.assertIsNone(units.pin_pitch([("1", 3, 3), ("2", 3, 3)]))
        self.assertEqual(units.pin_pitch(_tvw_r0402().pins), 3182.0)
        # Triangle: the median nearest-neighbour distance is the short
        # side (two of the three pins have it as their nearest).
        self.assertAlmostEqual(
            units.pin_pitch([("1", 0, 0), ("2", 100, 0), ("3", 100, 10)]),
            10.0)

    def test_pin_pitch_ignores_duplicate_pads(self):
        # Duplicate pad names at one position, or 1 unit apart, must not
        # collapse the pitch to 0 / 1 (that left an 82 mm PCIe finger as
        # a 0.003 mm sliver on the AORUS board).
        self.assertAlmostEqual(units.pin_pitch(_tvw_finger().pins), 3937.0)
        # Irregular single-row wafer: duplicates plus a few close pads;
        # the median is a typical spacing, not the smallest one.
        wafer = [("1", 0, 11811), ("6", 0, 5799), ("7", 0, 5799),
                 ("2", 0, 3937), ("12", 0, 2098), ("10", 0, 1575),
                 ("9", 0, -1575), ("11", 0, -2098), ("3", 0, -3937),
                 ("5", 0, -5799), ("8", 0, -5799), ("4", 0, -11811)]
        pitch = units.pin_pitch(wafer)
        self.assertGreater(pitch, 1500.0)
        self.assertLess(pitch, 2000.0)

    def test_pin_pitch_samples_large_shapes(self):
        # 42x42 BGA at 0.4 mm pitch: the sampled path must still report
        # the grid pitch, and stay fast.
        pins = [(str(i), (i % 42) * 1575.0, (i // 42) * 1575.0)
                for i in range(1764)]
        self.assertEqual(units.pin_pitch(pins), 1575.0)

    def test_pad_floor_and_pitch_scaling(self):
        # Pitch 10 -> 0.3 * 10 = 3 < floor -> floor (keeps mil-scale
        # boards and the existing board_export fixture unchanged).
        self.assertEqual(units.outline_pad_for_shape([("1", 0, 0), ("2", 10, 0)]),
                         units.OUTLINE_PAD_FLOOR)
        self.assertAlmostEqual(units.outline_pad_for_shape(_tvw_r0402().pins),
                               0.3 * 3182.0)
        # Same with the scale known: the 0402 is nowhere near the cap.
        self.assertAlmostEqual(units.outline_pad_for_shape(_tvw_r0402().pins, CMIL),
                               0.3 * 3182.0)
        # No pitch: 0.5 mm body when the scale is known, floor otherwise.
        self.assertAlmostEqual(units.outline_pad_for_shape([("1", 0, 0)], CMIL),
                               0.25 * CMIL)
        self.assertEqual(units.outline_pad_for_shape([("1", 0, 0)]),
                         units.OUTLINE_PAD_FLOOR)

    def test_grown_extent_is_capped_when_scale_is_known(self):
        # Two pads 100 mm apart (bus bar / shunt pair): 0.6 x pitch would
        # be a 60 mm wide hit box. Capped at OUTLINE_MAX_GROWN_EXTENT_MM.
        far = [("1", 0, 0), ("2", 0, 100 * CMIL)]
        self.assertAlmostEqual(units.outline_target_extent(far, CMIL),
                               units.OUTLINE_MAX_GROWN_EXTENT_MM * CMIL)
        # Without a scale there is nothing to cap against.
        self.assertAlmostEqual(units.outline_target_extent(far), 60 * CMIL)
        # A real choke (pitch 6.2 mm -> 3.7 mm body) stays under the cap.
        choke = [("1", 0, 0), ("2", 0, 24212)]
        self.assertAlmostEqual(units.outline_target_extent(choke, CMIL),
                               0.6 * 24212)

    def test_grown_extent_is_floored_when_scale_is_known(self):
        # 2-pin DFN with 0.43 mm pitch (RP75 ESD diodes): 0.6 x pitch is
        # 0.26 mm, under the 0.3 mm floor, so the body is 0.3 mm. The
        # same holds for the single-row wafer connectors on AORUS.
        dfn = [("1", 0, 0), ("2", 0, 1693)]
        self.assertAlmostEqual(units.outline_target_extent(dfn, CMIL),
                               units.OUTLINE_MIN_EXTENT_MM * CMIL)
        # Without a scale the pitch rule stands on its own.
        self.assertAlmostEqual(units.outline_target_extent(dfn), 0.6 * 1693)
        # The 0402 (0.48 mm) is above the floor and unaffected.
        self.assertAlmostEqual(units.outline_target_extent(_tvw_r0402().pins, CMIL),
                               0.6 * 3182.0)
        # Pitch-less shapes keep their own 0.5 mm fallback.
        self.assertAlmostEqual(units.outline_target_extent([("1", 0, 0)], CMIL),
                               units.OUTLINE_FALLBACK_EXTENT_MM * CMIL)

    def test_padded_bbox_only_grows_thin_axes(self):
        shape = _tvw_r0402()
        x0, y0, x1, y1 = units.padded_outline_bbox(shape.bbox(), shape.pins, CMIL)
        # Zero-width axis grows to 0.6 x pitch (~0.48 mm): wide enough to
        # see and click, close to a real 0402 body.
        self.assertGreaterEqual(x1 - x0, 0.4 * CMIL)
        self.assertAlmostEqual(x1 - x0, 0.6 * 3182.0)
        self.assertAlmostEqual((x0 + x1) / 2, 0.0)
        # The long axis already spans the pins: floor only.
        self.assertAlmostEqual(y0, -1591.0 - units.OUTLINE_PAD_FLOOR)
        self.assertAlmostEqual(y1, 1591.0 + units.OUTLINE_PAD_FLOOR)
        # A QFN whose bbox spans its body on both axes gets the floor.
        qfn = [(str(i), x, y) for i, (x, y) in enumerate(
            [(-5000, -5000), (5000, -5000), (5000, 5000), (-5000, 5000),
             (-5000, 0), (5000, 0), (0, -5000), (0, 5000)])]
        bb = units.padded_outline_bbox((-5000, -5000, 5000, 5000), qfn, CMIL)
        self.assertEqual(bb, (-5005.0, -5005.0, 5005.0, 5005.0))
        # Existing mil-scale fixture from test_board_export: unchanged,
        # with and without the (mil) scale.
        self.assertEqual(
            units.padded_outline_bbox((0, 0, 10, 0), [("1", 0, 0), ("2", 10, 0)]),
            (-5.0, -5.0, 15.0, 5.0))
        # With the (mil) scale known the 0.3 mm floor (11.8 mils) is
        # wider than 0.6 x the 10-mil pitch, so the thin axis grows to it.
        mil_floor = units.OUTLINE_MIN_EXTENT_MM * units.MILS_PER_MM
        bb = units.padded_outline_bbox((0, 0, 10, 0), [("1", 0, 0), ("2", 10, 0)],
                                       units.MILS_PER_MM)
        self.assertEqual((bb[0], bb[2]), (-5.0, 15.0))
        self.assertAlmostEqual(bb[3] - bb[1], mil_floor)
        self.assertAlmostEqual(bb[1], -bb[3])
        # The duplicated-pad finger gets a real body (1 mm pitch -> 0.6 mm).
        finger = _tvw_finger()
        fx0, _fy0, fx1, _fy1 = units.padded_outline_bbox(
            finger.bbox(), finger.pins, CMIL)
        self.assertAlmostEqual(fx1 - fx0, 0.6 * 3937.0)

    def test_shape_outline_bbox_is_memoised_per_shape_and_bbox(self):
        shape = _tvw_r0402()
        calls = {"n": 0}
        real_bbox = shape.bbox

        def counting_bbox():
            calls["n"] += 1
            return real_bbox()

        shape.bbox = counting_bbox  # type: ignore[method-assign]
        first = units.shape_outline_bbox(shape, CMIL)
        second = units.shape_outline_bbox(shape, CMIL)
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 2)  # bbox() is the cache key, so it is read
        # A different unit scale is a different cache key, and the first
        # entry survives it (no thrash between export and canvas).
        other = units.shape_outline_bbox(shape, None)
        self.assertEqual(other, first)  # 0402 is below the cap either way
        self.assertEqual(len(shape._outline_bbox_cache), 2)
        # An explicit local bbox is honoured per call, not served from
        # the first call's entry.
        wide = units.shape_outline_bbox(shape, CMIL, bbox=(0, 0, 50, 0))
        # 5000 x 3000 already exceeds the 0402's target extent (0.6 x
        # 3182 = 1909) on both axes, so only the floor pad applies.
        wider = units.shape_outline_bbox(shape, CMIL, bbox=(0, 0, 5000, 3000))
        self.assertNotEqual(wide, wider)
        self.assertEqual(wider, (-5.0, -5.0, 5005.0, 3005.0))
        self.assertEqual(units.shape_outline_bbox(shape, CMIL, bbox=(0, 0, 50, 0)),
                         wide)


class ExportedOutlineTests(unittest.TestCase):
    def test_tvw_0402_outline_is_at_least_0_4_mm_wide(self):
        shape = _tvw_r0402()
        comp = Component("R8031", 259000.0, 887500.0, "TOP", 0.0, shape.name)
        outline = board_export._component_outline(comp, shape, units_per_mm=CMIL)
        self.assertIsNotNone(outline)
        xs = [pt[0] for pt in outline]
        ys = [pt[1] for pt in outline]
        self.assertGreaterEqual(max(xs) - min(xs), 0.4 * CMIL)
        self.assertGreaterEqual(max(ys) - min(ys), 3182.0)
        self.assertAlmostEqual((max(xs) + min(xs)) / 2, comp.x)
        # Scale unknown: same result for this shape (below the cap).
        self.assertEqual(board_export._component_outline(comp, shape), outline)

    def test_tvw_0402_outline_rotated_90_is_wide_on_the_other_axis(self):
        shape = _tvw_r0402()
        comp = Component("R8081", 269500.0, 847000.0, "BOTTOM", 90.0, shape.name)
        outline = board_export._component_outline(comp, shape)
        xs = [pt[0] for pt in outline]
        ys = [pt[1] for pt in outline]
        self.assertGreaterEqual(max(ys) - min(ys), 0.4 * CMIL)
        self.assertGreaterEqual(max(xs) - min(xs), 3182.0)

    def _export(self, model: BoardModel) -> dict:
        saved_state = board_export._STATE.copy()
        try:
            with mock.patch.object(board_export, "parse_board", return_value=model):
                return json.loads(board_export._open_board("fixture.tvw", None))
        finally:
            board_export._STATE.clear()
            board_export._STATE.update(saved_state)

    def test_open_board_payload_carries_the_padded_outline(self):
        shape = _tvw_r0402()
        model = BoardModel(
            components={
                "R1": Component("R1", -30000.0, 0.0, "TOP", 0.0, shape.name),
                "R2": Component("R2", 30001.0, 0.0, "TOP", 0.0, shape.name),
            },
            shapes={shape.name: shape},
        )
        payload = self._export(model)
        self.assertEqual(payload["meta"]["units_per_mm"], CMIL)
        bbox = payload["components"][0]["bbox"]
        self.assertGreaterEqual(bbox[2] - bbox[0], 0.4 * CMIL)

    def test_open_board_outline_matches_desktop_for_pitchless_shapes(self):
        # 1-pin test point with a small bbox_override on a centi-mil
        # board: the export must use the same unit scale as the desktop
        # canvas, so both grow it to the 0.5 mm fallback body.
        tp = Shape(name="TP", pins=[("1", 0.0, 0.0)],
                   bbox_override=(-300.0, -300.0, 300.0, 300.0))
        model = BoardModel(
            components={
                "TP1": Component("TP1", 100000.0, 0.0, "TOP", 0.0, "TP"),
                "TP2": Component("TP2", 0.0, 60000.0, "TOP", 0.0, "TP"),
            },
            shapes={"TP": tp},
        )
        payload = self._export(model)
        self.assertEqual(payload["meta"]["units_per_mm"], CMIL)
        bbox = payload["components"][0]["bbox"]
        self.assertAlmostEqual(bbox[2] - bbox[0], 0.5 * CMIL)
        self.assertAlmostEqual(bbox[0], 100000.0 - 0.25 * CMIL)
        desktop = units.padded_outline_bbox(tp.bbox(), tp.pins, CMIL)
        self.assertAlmostEqual(bbox[2] - bbox[0], desktop[2] - desktop[0])


# ---- CanvasCommon through a tk-free stub ---------------------------------

class _StubCanvas(CanvasCommon):
    """CanvasCommon with identity projection on a 1000x1000 widget."""

    def __init__(self, board: BoardModel, view_layer: str = "TOP"):
        self.board = board
        self._view_layer = view_layer
        self._mirror_x = False
        self._rotation_quadrant = 0
        self._show_traces = False
        self._selected_refdes = None
        self._selected_pin = None
        self._show_all_pins = False
        self._pin_index = None
        # The synthetic boards span a few hundred units, which the real
        # canvas would read as mils (units_per_mm_for_span), so the
        # 0.3 mm floor is 11.8 units and the pitch rule drives the pad.
        self._units_per_mm_cache = units.MILS_PER_MM
        self.events = []
        self._on_select = lambda r: self.events.append(("select", r))
        self._on_pin_select = lambda p: self.events.append(("pin", p))
        self._on_layer_change = None
        xs = [c.x for c in board.components.values()] or [0.0]
        ys = [c.y for c in board.components.values()] or [0.0]
        self.bounds = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
        self.redraws = 0
        self.scale = 1.0

    def winfo_width(self):
        return 1000

    def winfo_height(self):
        return 1000

    def _project(self, x, y, w, h):
        return (x * self.scale, y * self.scale)

    def _unproject(self, sx, sy):
        return (sx / self.scale, sy / self.scale)

    def _schedule_redraw(self):
        self.redraws += 1

    def set_view_layer(self, layer):
        self.events.append(("layer", layer))
        self._view_layer = layer


def _sliver_board() -> BoardModel:
    # Two 2-pin parts 40 units apart whose pin bbox is zero wide, plus a
    # big 9-pin chip elsewhere. Identity projection => 1 unit == 1 px.
    sliver = Shape(name="S", pins=[("1", 0.0, 20.0), ("2", 0.0, -20.0)])
    big = Shape(name="B", pins=[(str(i), (i % 3) * 100.0, (i // 3) * 100.0)
                                for i in range(9)])
    return BoardModel(
        components={
            "R1": Component("R1", 100.0, 100.0, "TOP", 0.0, "S"),
            "R2": Component("R2", 140.0, 100.0, "TOP", 0.0, "S"),
            "U1": Component("U1", 600.0, 600.0, "TOP", 0.0, "B"),
            "R3": Component("R3", 100.0, 300.0, "BOTTOM", 0.0, "S"),
        },
        shapes={"S": sliver, "B": big},
    )


class CanvasPickTests(unittest.TestCase):
    def test_polygon_world_uses_shared_pad_rule(self):
        cv = _StubCanvas(_sliver_board())
        poly = cv._component_polygon_world(cv.board.components["R1"])
        xs = [p[0] for p in poly]
        # pitch 40 -> target extent 24 (0.6 x pitch) on the zero-width axis.
        self.assertAlmostEqual(max(xs) - min(xs), 24.0)
        self.assertAlmostEqual(sum(xs) / 4, 100.0)

    def test_click_inside_and_near_miss_select_the_sliver(self):
        cv = _StubCanvas(_sliver_board())
        # Inside the (now 24-wide) outline.
        self.assertEqual(cv._find_component_at(100, 100), "R1")
        # 4 px outside the right edge (x = 112): near-miss tolerance.
        self.assertEqual(cv._find_component_at(116, 100), "R1")
        # Between R1 (edge at 112) and R2 (edge at 128): nearest wins.
        self.assertEqual(cv._find_component_at(118, 100), "R1")
        self.assertEqual(cv._find_component_at(123, 100), "R2")
        # Far from everything: nothing.
        self.assertIsNone(cv._find_component_at(400, 100))
        # Other layer's parts are never candidates.
        self.assertIsNone(cv._find_component_at(100, 300))

    def test_dist_to_poly_edges(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.assertAlmostEqual(CanvasCommon._dist_to_poly_edges(15, 5, sq), 5.0)
        self.assertAlmostEqual(CanvasCommon._dist_to_poly_edges(13, 14, sq), 5.0)
        self.assertAlmostEqual(CanvasCommon._dist_to_poly_edges(5, 5, sq), 5.0)


class AllPinsTests(unittest.TestCase):
    def test_pin_index_and_gate(self):
        cv = _StubCanvas(_sliver_board())
        idx = cv._ensure_pin_index()
        self.assertEqual(sorted(idx), ["BOTTOM", "TOP"])
        top = idx["TOP"]
        self.assertEqual(len(top["xs"]), 2 + 2 + 9)
        self.assertEqual(top["median_pitch"], 40.0)
        comps = {c.refdes: (minx, miny, maxx, maxy, pitch, n)
                 for c, _s, minx, miny, maxx, maxy, pitch, n in top["comps"]}
        self.assertEqual(comps["R2"], (140.0, 80.0, 140.0, 120.0, 40.0, 2))
        self.assertEqual(comps["U1"][4:], (100.0, 9))
        self.assertEqual(sorted(top["groups"]), ["B", "S"])
        self.assertEqual(len(top["groups"]["S"]["xs"]), 4)
        # Off by default; on with an outer layer and 40 px pitch => visible.
        self.assertFalse(cv._all_pins_visible(1000, 1000))
        cv.set_show_all_pins(True)
        self.assertTrue(cv.show_all_pins)
        self.assertEqual(cv.redraws, 1)
        self.assertTrue(cv._all_pins_visible(1000, 1000))
        self.assertIn("pads: ON", cv._all_pins_hint(1000, 1000))
        # Inner-layer view: never drawn, never clickable.
        cv._view_layer = "INNER_1"
        self.assertFalse(cv._all_pins_visible(1000, 1000))
        self.assertIsNone(cv._find_any_pin_at(100, 120))

    def test_zoom_gate_uses_median_pitch(self):
        cv = _StubCanvas(_sliver_board())
        cv.set_show_all_pins(True)
        # Shrink the projection so 40 world units become 2 px.
        cv.scale = 0.05
        self.assertFalse(cv._all_pins_visible(1000, 1000))
        self.assertIn("zoom in", cv._all_pins_hint(1000, 1000))
        self.assertIsNone(cv._find_any_pin_at(5, 6))

    def test_gate_caps_visible_pin_count(self):
        # 60 x 60 grid of 4-pin parts = 14,400 pins at a comfortable
        # 10 px pitch: over the item budget at fit zoom (and every part
        # has the same pin count, so nothing can be dropped to fit),
        # fine once three quarters of them are off-screen.
        quad = Shape(name="Q", pins=[("1", 0, 0), ("2", 10, 0),
                                     ("3", 0, 10), ("4", 10, 10)])
        comps = {}
        for i in range(60):
            for j in range(60):
                r = f"C{i}_{j}"
                comps[r] = Component(r, 20.0 + i * 16, 20.0 + j * 16, "TOP",
                                     0.0, "Q")
        cv = _StubCanvas(BoardModel(components=comps, shapes={"Q": quad}))
        cv.set_show_all_pins(True)
        entry = cv._ensure_pin_index()["TOP"]
        self.assertEqual(cv._visible_pin_estimate(entry, 1000, 1000, 1.0), 14400)
        self.assertEqual(cv._overlay_budget(entry, 1000, 1000, 1.0), (14400, 0, 0))
        self.assertFalse(cv._all_pins_visible(1000, 1000))
        self.assertIn("zoom in", cv._all_pins_hint(1000, 1000))
        self.assertIsNone(cv._find_any_pin_at(20, 20))
        # Zoom 2x: the viewport (500 world units) holds ~31x31 parts;
        # the parts cut by the viewport edge count by their visible
        # fraction, so the estimate is a little under 31.25^2 x 4.
        cv.scale = 2.0
        n = cv._visible_pin_estimate(entry, 1000, 1000, 2.0)
        self.assertLess(n, cv.ALL_PINS_MAX_VISIBLE)
        self.assertGreater(n, 3000)
        self.assertEqual(cv._overlay_budget(entry, 1000, 1000, 2.0)[2], None)
        self.assertTrue(cv._all_pins_visible(1000, 1000))
        self.assertIn("click any pad", cv._all_pins_hint(1000, 1000))
        self.assertEqual(cv._find_any_pin_at(40, 40), ("C0_0", "1"))

    def test_budget_drops_the_biggest_parts_first(self):
        # The 4-pin grid of the previous test at 2x (~3.8k pins in view)
        # plus a 1,000-ball 2-unit-pitch BGA sitting in a hole of the
        # grid: together they exceed the 4k budget, so the BGA is left
        # out — not drawn, not clickable, named in the hint — while
        # every passive stays.
        quad = Shape(name="Q", pins=[("1", 0, 0), ("2", 10, 0),
                                     ("3", 0, 10), ("4", 10, 10)])
        bga = Shape(name="G", pins=[(str(i), (i % 32) * 2.0, (i // 32) * 2.0)
                                    for i in range(1000)])
        comps = {}
        for i in range(60):
            for j in range(60):
                x, y = 20.0 + i * 16, 20.0 + j * 16
                if 50 <= x <= 130 and 50 <= y <= 130:
                    continue  # room for the BGA
                r = f"C{i}_{j}"
                comps[r] = Component(r, x, y, "TOP", 0.0, "Q")
        comps["U9"] = Component("U9", 60.0, 60.0, "TOP", 0.0, "G")
        cv = _StubCanvas(BoardModel(components=comps, shapes={"Q": quad, "G": bga}))
        cv.set_show_all_pins(True)
        cv.scale = 2.0
        entry = cv._ensure_pin_index()["TOP"]
        in_view, drawn, max_pins = cv._overlay_budget(entry, 1000, 1000, 2.0)
        self.assertGreater(in_view, cv.ALL_PINS_MAX_VISIBLE)
        self.assertEqual(max_pins, 4)
        self.assertLessEqual(drawn, cv.ALL_PINS_MAX_VISIBLE)
        self.assertTrue(cv._all_pins_visible(1000, 1000))
        self.assertIn("parts over 4 pins hidden", cv._all_pins_hint(1000, 1000))
        # BGA ball "0" sits at world (60, 60) = screen (120, 120): not
        # clickable while the part is out of budget; the passive at
        # world (20, 20) still is.
        self.assertIsNone(cv._find_any_pin_at(120, 120))
        self.assertEqual(cv._find_any_pin_at(40, 40), ("C0_0", "1"))
        # Zoom into the BGA (viewport 125 world units): a few hundred
        # pins in view, everything fits, the ball is clickable.
        cv.scale = 8.0
        in_view, drawn, max_pins = cv._overlay_budget(entry, 1000, 1000, 8.0)
        self.assertIsNone(max_pins)
        self.assertEqual(in_view, drawn)
        self.assertLess(in_view, cv.ALL_PINS_MAX_VISIBLE)
        self.assertEqual(cv._find_any_pin_at(480, 480), ("U9", "0"))
        self.assertIn("click any pad", cv._all_pins_hint(1000, 1000))
        # A frame with a raised budget (the GL tier) draws everything.
        cv.scale = 2.0
        cv.ALL_PINS_MAX_VISIBLE = 250_000
        self.assertEqual(cv._overlay_budget(entry, 1000, 1000, 2.0)[2], None)
        self.assertEqual(cv._find_any_pin_at(120, 120), ("U9", "0"))

    def test_per_component_pitch_predicate(self):
        # A fine-pitch BGA next to the passives: at a zoom where the
        # passives (pitch 40) resolve but the BGA (pitch 2) is a smear,
        # the BGA's pins are neither drawn nor clickable, so a click on
        # it falls through to the ordinary component pick.
        board = _sliver_board()
        bga = Shape(name="G", pins=[(str(i), (i % 10) * 2.0, (i // 10) * 2.0)
                                    for i in range(100)])
        board.shapes["G"] = bga
        board.components["U2"] = Component("U2", 300.0, 100.0, "TOP", 0.0, "G")
        cv = _StubCanvas(board)
        cv.set_show_all_pins(True)
        self.assertTrue(cv._all_pins_visible(1000, 1000))
        self.assertFalse(cv._pins_resolvable(2.0, 1.0))
        self.assertTrue(cv._pins_resolvable(40.0, 1.0))
        self.assertTrue(cv._pins_resolvable(None, 1.0))
        self.assertIsNone(cv._find_any_pin_at(304, 104))
        entry = cv._ensure_pin_index()["TOP"]
        # The BGA's 100 pins are not part of the item estimate either.
        self.assertEqual(cv._visible_pin_estimate(entry, 1000, 1000, 1.0), 13)
        cv.scale = 2.0  # pitch 4 px: now resolvable and clickable
        # Screen (608, 208) is world (304, 104): U2's local (4, 4) = pin 22.
        self.assertEqual(cv._find_any_pin_at(608, 208), ("U2", "22"))
        self.assertEqual(cv._find_any_pin_at(605, 205), ("U2", "11"))

    def test_gate_uses_proxy_pitch_on_pitchless_layers(self):
        # A layer of single-pin parts has no median pitch; the gate uses
        # sqrt(pin-cloud area / pins) instead of passing unconditionally.
        tp = Shape(name="T", pins=[("1", 0.0, 0.0)])
        comps = {f"TP{i}": Component(f"TP{i}", (i % 20) * 50.0, (i // 20) * 50.0,
                                     "TOP", 0.0, "T") for i in range(400)}
        cv = _StubCanvas(BoardModel(components=comps, shapes={"T": tp}))
        cv.set_show_all_pins(True)
        entry = cv._ensure_pin_index()["TOP"]
        self.assertIsNone(entry["median_pitch"])
        self.assertAlmostEqual(entry["proxy_pitch"], 47.5)
        self.assertTrue(cv._all_pins_visible(1000, 1000))
        cv.scale = 0.05  # 2.4 px proxy pitch -> gated
        self.assertFalse(cv._all_pins_visible(1000, 1000))

    def test_single_click_selects_component_and_pin(self):
        cv = _StubCanvas(_sliver_board())
        cv.set_show_all_pins(True)
        cv.redraws = 0
        # R2 pin 1 is at (140, 120); click 3 px away.
        self.assertEqual(cv._find_any_pin_at(142, 122), ("R2", "1"))
        self.assertTrue(cv._click_any_pin(142, 122))
        self.assertEqual((cv.selected_refdes, cv.selected_pin), ("R2", "1"))
        self.assertEqual(cv.events, [("select", "R2"), ("pin", "1")])
        self.assertEqual(cv.redraws, 1)
        # Same component, other pin: only the pin callback fires.
        cv.events.clear()
        self.assertTrue(cv._click_any_pin(140, 80))
        self.assertEqual(cv.events, [("pin", "2")])
        # Same pin again: nothing fires, nothing redraws.
        cv.events.clear()
        cv.redraws = 0
        self.assertTrue(cv._click_any_pin(140, 80))
        self.assertEqual(cv.events, [])
        self.assertEqual(cv.redraws, 0)
        # Beyond PIN_CLICK_RADIUS_PX of every pin: not handled.
        self.assertFalse(cv._click_any_pin(400, 400))
        # Toggle off: the pin path is disabled even for a direct hit.
        cv.set_show_all_pins(False)
        self.assertIsNone(cv._find_any_pin_at(140, 120))

    def test_pad_beats_via_when_closer(self):
        # Traces on, a via 5 px from R2 pin 1 (at 140,120). A click on
        # the pad must select the pad, not flip the layer; a click
        # nearer the via still punches through; with pads off the via
        # wins as before.
        board = _sliver_board()
        board._topology = SimpleNamespace(vias=[SimpleNamespace(x=145.0, y=120.0)])
        cv = _StubCanvas(board)
        cv._show_traces = True
        cv.set_show_all_pins(True)
        cv.events.clear()
        self.assertTrue(cv._click_via_or_pad(141, 120))
        self.assertEqual(cv.events, [("select", "R2"), ("pin", "1")])
        self.assertEqual(cv._view_layer, "TOP")
        cv.events.clear()
        self.assertTrue(cv._click_via_or_pad(146, 120))
        self.assertEqual(cv.events, [("layer", "BOTTOM")])
        cv._view_layer = "TOP"
        cv.events.clear()
        cv.set_show_all_pins(False)
        self.assertTrue(cv._click_via_or_pad(141, 120))
        self.assertEqual(cv.events, [("layer", "BOTTOM")])
        # Nothing near: not consumed.
        cv._view_layer = "TOP"
        self.assertFalse(cv._click_via_or_pad(400, 400))


# ---- Pieces that need a real tk interpreter ------------------------------

def _tk_root():
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


class NetPanelFocusTests(unittest.TestCase):
    def test_focus_pin_moves_highlight_without_rebuilding(self):
        root = _tk_root()
        if root is None:
            self.skipTest("no display")
        try:
            from src.ui_panels import NetInfoPanel
            shape = Shape(name="S", pins=[("1", 0, 0), ("2", 10, 0)])
            model = BoardModel(
                components={
                    "R1": Component("R1", 0, 0, "TOP", 0, "S"),
                    "R2": Component("R2", 50, 0, "BOTTOM", 0, "S"),
                },
                signals={"NET": [("R1", "1"), ("R2", "2")]},
                shapes={"S": shape},
            )
            panel = NetInfoPanel(root, model)
            panel.show_net("NET", focus_pin=("R1", "1"))
            rows = panel.tree.get_children()
            self.assertEqual(panel._highlighted_pin_iid, "R1__1")
            self.assertIn("highlight", panel.tree.item("R1__1", "tags"))
            # Same net again with another focus: rows are the same objects,
            # highlight moved.
            panel.show_net("NET", focus_pin=("R2", "2"))
            self.assertEqual(panel.tree.get_children(), rows)
            self.assertNotIn("highlight", panel.tree.item("R1__1", "tags"))
            self.assertIn("highlight", panel.tree.item("R2__2", "tags"))
            self.assertEqual(panel.tree.selection(), ("R2__2",))
            panel.focus_pin(None, None)
            self.assertIsNone(panel._highlighted_pin_iid)
            self.assertNotIn("highlight", panel.tree.item("R2__2", "tags"))
        finally:
            root.destroy()


class StartupLauncherTests(unittest.TestCase):
    def test_launcher_rows_flag_missing_files(self):
        from src.viewer import _launcher_rows
        with tempfile.TemporaryDirectory() as d:
            present = Path(d) / "a.tvw"
            present.write_bytes(b"")
            rows = _launcher_rows([str(present), str(Path(d) / "gone.cad")])
        self.assertEqual([(p.name, ok) for p, ok in rows],
                         [("a.tvw", True), ("gone.cad", False)])

    def test_launcher_opens_selected_and_refuses_missing(self):
        root = _tk_root()
        if root is None:
            self.skipTest("no display")
        try:
            from src.viewer import StartupLauncher
            with tempfile.TemporaryDirectory() as d:
                present = Path(d) / "a.tvw"
                present.write_bytes(b"")
                missing = Path(d) / "gone.cad"
                launcher = StartupLauncher(root, [str(missing), str(present)])
                # First EXISTING row is preselected, missing rows are tagged.
                self.assertEqual(launcher.selected_row(), (present, True))
                self.assertIn("missing", launcher.tree.item("0", "tags"))
                launcher._open_selected()
                self.assertEqual(launcher.result, present)
                # Selecting the missing row and opening it is refused.
                launcher.result = None
                launcher.tree.selection_set("0")
                with mock.patch("src.viewer.messagebox.showwarning") as warn:
                    launcher._open_selected()
                self.assertIsNone(launcher.result)
                self.assertEqual(warn.call_count, 1)
                launcher._quit()
                self.assertIsNone(launcher.result)
        finally:
            root.destroy()

    def test_launcher_return_follows_focus_and_double_click_needs_a_row(self):
        root = _tk_root()
        if root is None:
            self.skipTest("no display")
        try:
            from src.viewer import StartupLauncher
            with tempfile.TemporaryDirectory() as d:
                present = Path(d) / "a.tvw"
                present.write_bytes(b"")
                launcher = StartupLauncher(root, [str(present)])
                # Enter with the list focused opens the selection.
                with mock.patch.object(root, "focus_get", return_value=launcher.tree):
                    launcher._on_return()
                self.assertEqual(launcher.result, present)
                # Enter on a focused button activates THAT button.
                launcher.result = present
                with mock.patch.object(root, "focus_get",
                                       return_value=launcher.quit_btn):
                    launcher._on_return()
                self.assertIsNone(launcher.result)
                # The button captured the bound `_browse` at build time,
                # so the file dialog itself is what must be stubbed.
                with mock.patch("src.viewer.filedialog.askopenfilename",
                                return_value="") as dialog, \
                        mock.patch.object(root, "focus_get",
                                          return_value=launcher.browse_btn):
                    launcher._on_return()
                self.assertEqual(dialog.call_count, 1)
                self.assertIsNone(launcher.result)
                # Double-click: only a row region opens.
                launcher.result = None
                tree = launcher.tree
                with mock.patch.object(tree, "identify_region", return_value="heading"):
                    launcher._on_tree_double_click(SimpleNamespace(x=10, y=5))
                self.assertIsNone(launcher.result)
                with mock.patch.object(tree, "identify_region", return_value="nothing"):
                    launcher._on_tree_double_click(SimpleNamespace(x=10, y=400))
                self.assertIsNone(launcher.result)
                with mock.patch.object(tree, "identify_region", return_value="tree"), \
                        mock.patch.object(tree, "identify_row", return_value=""):
                    launcher._on_tree_double_click(SimpleNamespace(x=10, y=400))
                self.assertIsNone(launcher.result)
                with mock.patch.object(tree, "identify_region", return_value="cell"), \
                        mock.patch.object(tree, "identify_row", return_value="0"):
                    launcher._on_tree_double_click(SimpleNamespace(x=10, y=30))
                self.assertEqual(launcher.result, present)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
