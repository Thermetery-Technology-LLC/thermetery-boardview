# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Unit heuristics shared by the desktop canvases and the Android export.

Boardview formats store coordinates in whatever unit the exporter chose
and carry no unit metadata, so scale is inferred from the coordinate
span of the placed components. Every parser normalises to either mils
(GENCAD/BRD/FZ/XZZ/ASC) or centi-mils (TVW), which the span separates
cleanly: no real board is 50,000 mils (1.27 m) wide.

Stdlib-only on purpose — board_export.py runs under Chaquopy on Android
where tkinter doesn't exist, so nothing UI-flavoured may leak in here.
"""

from typing import Optional

MILS_PER_MM = 39.37
CENTIMILS_PER_MM = 3937.0

# Component-extent span (in file units) above which coordinates are
# taken to be centi-mils rather than mils.
CENTIMIL_SPAN_THRESHOLD = 50_000


def units_per_mm_for_span(span: float) -> float:
    """File-units-per-mm inferred from the component coordinate span."""
    return CENTIMILS_PER_MM if span > CENTIMIL_SPAN_THRESHOLD else MILS_PER_MM


# ----- Component outline padding -------------------------------------------
#
# Component outlines are derived from the shape's pin bbox. That bbox is
# degenerate for 2-pin chip passives whose pins sit on one axis — a TVW
# 0402 has pins at (0, +/-1591) with zero width — and a fixed 5-unit pad
# (0.0025 mm in centi-mils) left them as hairlines that were impossible
# to see or click. The rule below grows any axis thinner than a fraction
# of the shape's pin pitch up to that fraction, which reproduces the
# physical body width of chip passives closely (0402: 0.48 mm,
# 0805: 1.2 mm, 1206: 1.7 mm) without inflating parts whose bbox already
# spans their body. Shared by the desktop canvases and board_export so
# the Android app draws and hit-tests the same outline.
#
# "Pitch" is the median per-pin nearest-neighbour distance, not the
# global minimum: TVW shapes routinely list the same pad twice under two
# names (a PCIe finger has A1 and 1 at one position, sometimes 1 unit
# apart) and a global minimum collapsed to that duplicate, leaving the
# 82 mm connector a 0.003 mm sliver. Distances below 1 % of the pin
# cloud's extent are treated as duplicates and ignored. When the unit
# scale is known the grown extent is also clamped to a mm range so a
# tiny-pitch part is never thinner than 0.3 mm and a 2-pin part whose
# pads are 100 mm apart does not become a 60 mm body.

# Absolute floor on the per-side pad, in file units. Pre-existing value.
OUTLINE_PAD_FLOOR = 5.0
# Target minimum extent of an outline axis, as a fraction of the shape's
# pin pitch (so the per-side pad on a zero-width axis is half).
OUTLINE_MIN_EXTENT_PITCH_FRACTION = 0.6
# Fallback body size (mm) for shapes with no usable pitch (single pin
# or coincident pins) when the unit scale is known.
OUTLINE_FALLBACK_EXTENT_MM = 0.5
# Lower bound (mm) on the extent a thin axis is grown to, when the unit
# scale is known: a sliver-thin part whose pitch is tiny (a 12-pin
# single-row wafer whose pads collapse to one line, a 2-pin DFN with
# 0.43 mm pitch) still gets a body one can see and click.
OUTLINE_MIN_EXTENT_MM = 0.3
# Upper bound (mm) on the extent a thin axis is grown to, when the unit
# scale is known. Bounds the hit box of a 2-pin shape whose pins are far
# apart (a bus bar or shunt pair). Real parts on the sample boards grow
# to 3.7 mm (7343 tantalums), 3.9 mm (2512 resistors) and 4.7-5.0 mm
# (10LC4 chokes), all at or under the cap, so a tighter 2 mm cap would
# shrink real bodies; this one only catches the far-apart pairs.
OUTLINE_MAX_GROWN_EXTENT_MM = 5.0
# Nearest-neighbour distances below this fraction of the pin cloud's
# larger extent are duplicate pads, not pitch.
_PITCH_DUPLICATE_FRACTION = 0.01
# Pairwise search is O(n^2); above this pin count only a sample of
# anchor pins is measured (the pad is irrelevant on big chips whose
# bbox already spans the body).
_PITCH_EXACT_MAX_PINS = 64
_PITCH_SAMPLE_ANCHORS = 32


def pin_pitch(pins) -> Optional[float]:
    """Typical pin pitch of a shape: the median over pins of the distance
    to the nearest OTHER pad, in the shape's own units. Pads closer than
    1 % of the pin cloud's extent (duplicate names on one pad) do not
    count as neighbours. None when fewer than two distinct pad positions
    exist. `pins` is the Shape.pins list of (name, dx, dy) tuples."""
    pts = []
    for p in pins:
        try:
            pts.append((float(p[1]), float(p[2])))
        except (TypeError, ValueError, IndexError):
            continue
    n = len(pts)
    if n < 2:
        return None
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    eps2 = (extent * _PITCH_DUPLICATE_FRACTION) ** 2
    if n > _PITCH_EXACT_MAX_PINS:
        anchors = pts[:: max(1, n // _PITCH_SAMPLE_ANCHORS)][:_PITCH_SAMPLE_ANCHORS]
    else:
        anchors = pts
    nearest = []
    for ax, ay in anchors:
        best = None
        for bx, by in pts:
            dx = bx - ax
            dy = by - ay
            d2 = dx * dx + dy * dy
            if d2 <= eps2:
                continue
            if best is None or d2 < best:
                best = d2
        if best is not None:
            nearest.append(best)
    if not nearest:
        return None
    nearest.sort()
    return nearest[len(nearest) // 2] ** 0.5


def outline_target_extent(pins, units_per_mm: Optional[float] = None) -> float:
    """Minimum extent (file units) an outline axis is grown to:
    OUTLINE_MIN_EXTENT_PITCH_FRACTION x pin pitch, clamped to
    [OUTLINE_MIN_EXTENT_MM, OUTLINE_MAX_GROWN_EXTENT_MM] when the unit
    scale is known, and never below 2 x OUTLINE_PAD_FLOOR. Shapes
    without a usable pitch fall back to OUTLINE_FALLBACK_EXTENT_MM when
    the scale is known, else the floor."""
    pitch = pin_pitch(pins)
    floor = 2.0 * OUTLINE_PAD_FLOOR
    if pitch is None:
        if units_per_mm:
            return max(floor, OUTLINE_FALLBACK_EXTENT_MM * units_per_mm)
        return floor
    target = OUTLINE_MIN_EXTENT_PITCH_FRACTION * pitch
    if units_per_mm:
        target = min(target, OUTLINE_MAX_GROWN_EXTENT_MM * units_per_mm)
        floor = max(floor, OUTLINE_MIN_EXTENT_MM * units_per_mm)
    return max(floor, target)


def outline_pad_for_shape(pins, units_per_mm: Optional[float] = None) -> float:
    """Per-side pad (file units) that an outline axis with zero extent
    needs to reach outline_target_extent(): half of it."""
    return outline_target_extent(pins, units_per_mm) / 2.0


def padded_outline_bbox(bbox, pins, units_per_mm: Optional[float] = None):
    """Grow a shape's local pin bbox into the outline rectangle to draw.

    Every side gets at least OUTLINE_PAD_FLOOR. An axis whose extent is
    below outline_target_extent() is grown symmetrically up to that
    target, so a zero-width 0402 becomes ~0.6 x pitch wide while a QFN
    whose pins already span its body only gets the floor."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    target = outline_target_extent(pins, units_per_mm)
    pad_x = max(OUTLINE_PAD_FLOOR, (target - (x1 - x0)) / 2.0)
    pad_y = max(OUTLINE_PAD_FLOOR, (target - (y1 - y0)) / 2.0)
    return (x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y)


def shape_outline_bbox(shape, units_per_mm: Optional[float] = None,
                       bbox=None):
    """padded_outline_bbox() for a Shape, memoised on the shape object.

    Shapes are immutable after parsing and a board instantiates the same
    footprint hundreds of times, so results are cached on the instance
    in a small dict keyed by (units_per_mm, bbox) — the canvases call
    this per component per redraw, and the desktop (real scale) and the
    export (which may pass None) can share one process. `bbox` lets a
    caller that already has shape.bbox() skip the extra call; a caller
    passing a different local bbox gets its own entry. Objects that
    refuse new attributes simply aren't cached."""
    if bbox is None:
        bbox = shape.bbox()
    key = (units_per_mm, tuple(float(v) for v in bbox))
    cache = getattr(shape, "_outline_bbox_cache", None)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit
    padded = padded_outline_bbox(bbox, shape.pins, units_per_mm)
    try:
        if cache is None:
            cache = {}
            shape._outline_bbox_cache = cache
        if len(cache) >= 8:
            cache.clear()
        cache[key] = padded
    except (AttributeError, TypeError):
        pass
    return padded


def shape_pin_pitch(shape) -> Optional[float]:
    """pin_pitch(shape.pins), memoised on the shape object."""
    cached = getattr(shape, "_pin_pitch_cache", None)
    if cached is not None:
        return cached[0]
    pitch = pin_pitch(shape.pins)
    try:
        shape._pin_pitch_cache = (pitch,)
    except (AttributeError, TypeError):
        pass
    return pitch


__all__ = [
    "CENTIMILS_PER_MM",
    "CENTIMIL_SPAN_THRESHOLD",
    "MILS_PER_MM",
    "OUTLINE_FALLBACK_EXTENT_MM",
    "OUTLINE_MAX_GROWN_EXTENT_MM",
    "OUTLINE_MIN_EXTENT_MM",
    "OUTLINE_MIN_EXTENT_PITCH_FRACTION",
    "OUTLINE_PAD_FLOOR",
    "outline_pad_for_shape",
    "outline_target_extent",
    "padded_outline_bbox",
    "pin_pitch",
    "shape_outline_bbox",
    "shape_pin_pitch",
    "units_per_mm_for_span",
]
