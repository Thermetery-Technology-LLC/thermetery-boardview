# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Compal/Lenovo TVW variant decoder.

The Compal variant (used by Compal-manufactured laptops, notably the
Lenovo Thinkpad T-series via NM-B501 and related boards) deviates from
the Gigabyte TVW convention in several structural places:

  * Chip enumeration uses a multi-source union: Region 3 (primary,
    chip records anchored by `00 00 00 00 + Pascal-refdes`) plus the
    cap-section (the historical `0x01 + Pascal-dev + Pascal-fp` chips
    that tvw_parser._chip_headers already finds) plus Region 1
    (a supervised layer-flag table identified by a `0xbb800 / 0x12c00`
    constants signature).
  * Layer pads are 19-byte stride records for ALL 10 copper layers,
    not Gigabyte's 38-byte (and 54-byte through-hole) format. A single
    38-byte stride region exists but only carries GND stitching vias.
  * Master footprint pool starts ~234 KB earlier in the file than the
    Gigabyte convention would predict — around 0xbd0000 in T480.
  * Chip layer is not encoded as a single byte inside the chip R3
    record. It comes from a 3-source chain (R1 supervised → cap-section
    trailer byte +20 → master record `_B` suffix).
  * The canonical pin-position transform uses the chip's bbox-anchor
    (f2, f3) at after-Pascal +16..+23 — NOT the chip's world position
    at +0..+7 — as the origin for adding master-local pin offsets.

See `TVW_FORMAT.html` (in this repo) sections 5-13 for the full format
spec with byte layouts and ground-truth anchors.

This module is the *minimum-viable* Compal decoder: it produces chips
with correct positions, rotations, layers, and shape geometry. Pin-net
mapping (model.signals) is intentionally NOT populated here — that
requires matching predicted pin world coords against the 19-byte layer
pad records, which is its own substantial code path and lands in a
follow-up commit. The warnings list surfaces this fact to the viewer
so users know net browsing won't return results yet.

Verified against Lenovo Thinkpad T480 NM-B501R10 (MD5
6983e8afd4af43829ec210c1eef0136f) and Lenovo ThinkPad T470p LCFC
NM-B071 (MD5 facab74b19f8de47ad7f1987551170ac). The two differ in
three ways, each resolved from the file's own data rather than a
per-board table:

  * Region offsets: T470p's net table / R3 chips / master pool sit
    ~2.5 MB earlier. `_resolve_layout` keeps the T480 offsets when
    they validate, else `_autodetect_layout` finds the pool start by
    matching R3 footprint names against master names.
  * Cap-section layer byte: +20 on T480, +9 on T470p
    (`_scan_cap_section_layers` picks the one that agrees with R1).
  * Pin frame: T470p uses T480's connector frame for every chip
    (`_choose_pin_frame` picks whichever lands more pins on pads).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import math
import re

from .parsers.gencad_parser import BoardModel, Component, Shape

# Optional numpy acceleration for the three whole-file byte scanners
# (_scan_r1_layers, _build_pad_index, _scan_net_names). These are pure
# `while i < len(data)` loops over all ~12.8M bytes of a Compal TVW and
# dominate cold-parse wall time (~71%). numpy lets us collapse the 12.8M
# Python iterations to the ~0.85M-1.8M candidate offsets that survive a
# vectorized prefilter (np.frombuffer + boolean masks + np.nonzero)
# before any per-byte validation runs.
#
# This is ADDITIVE and OPTIONAL: if numpy is missing or the fast path
# raises for any reason, every scanner transparently falls back to the
# byte-for-byte-identical pure-Python implementation kept below as
# `_<name>_py`. The fast paths are written to produce the exact same
# result (dict / list) as the reference loops, including ordering and
# first-seen dedup semantics.
try:
    import numpy as _np
    _NUMPY_OK = True
except Exception:  # pragma: no cover - numpy is normally present
    _np = None
    _NUMPY_OK = False


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# 8-byte signature of Region 1 layer-flag records: the chip's
# `0xbb800 / 0x12c00` constants packed at after-Pascal +12..+19. Used
# both for variant detection (in tvw_parser._detect_variant) and for
# scanning the supervised layer-flag table.
_R1_SIGNATURE = b"\xb8\x0b\x00\x00\x2c\x01\x00\x00"

# 9-byte signature marking the start of a master footprint record:
# 8 zero bytes followed by 0x01, then a Pascal-prefixed footprint name.
_MASTER_SIGNATURE = b"\x00\x00\x00\x00\x00\x00\x00\x00\x01"

# T480 (NM-B501) layout: where the master footprint pool starts, the
# Region 3 chip-record range and the net-name table range. These are
# tried first and kept only if they validate (see `_resolve_layout`);
# other boards (T470p NM-B071 puts the same regions ~2.5 MB earlier)
# fall through to `_autodetect_layout`, which finds them from the data.
_MASTER_POOL_START_HINT = 0xbd0000
_R3_SEARCH_START = 0xa00000
_R3_SEARCH_END = 0xc00000
_NET_SEARCH_START = 0xa00000
_NET_SEARCH_END = 0xc40000

# A layout is accepted when at least this fraction of its R3 chips name
# the same footprint (packed slot 3) as the master their fp_id points at.
_LAYOUT_MIN_FP_AGREEMENT = 0.9

# Sentinel byte found at chip after-Pascal +44 in ALL valid Compal R3
# records. The bytes immediately following vary by chip type (ICs use
# `01 01 2a 00 00` then a Pascal footprint name; passives pack their
# value as a small Pascal string first), so we use the single byte at
# +44 as a cheap pre-filter and rely on the fp_id → master lookup to
# reject false positives.
_CHIP_PRELUDE_SENTINEL = 0x01

# Test-point fp_ids — these chip records have no electrical body, just
# outline placeholders for TB_TP1..TB_TP6. Filtering them keeps the
# component count meaningful.
_TEST_POINT_FP_IDS = frozenset({235, 236, 237, 238, 239, 240})

# chip_class values (see `_Chip.chip_class`) for test points and
# fiducials — the autodetected path's structural stand-in for the
# T480-only `_TEST_POINT_FP_IDS`.
_PINLESS_MARKER_CLASSES = frozenset({18, 29})

# Refdes patterns that are pin sub-records nested inside larger chips,
# NOT standalone components. The `H`, `FL`, `FLJ` prefixes are pin-name
# conventions used inside connectors and ICs on T480. Note: many `P*`
# compound prefixes (PQ, PD, PL, PU, PR, …) are NOT pin sub-records on
# T480 — they're real chip refdes (MOSFETs, diodes, etc.). We rely on
# the structural pre-filter (byte +44 sentinel + valid fp_id) plus the
# `_KNOWN_PIN_PATTERNS` regex below to keep the chip set clean.
_KNOWN_PIN_PATTERNS = re.compile(r"^(H[0-9]+|FL[0-9]+|FLJ[0-9]+)$")


# --------------------------------------------------------------------------
# Data classes (parser-internal, not exported)
# --------------------------------------------------------------------------

@dataclass(slots=True)
class _Master:
    """One footprint master record. Built by `_scan_master_pool`."""
    idx: int            # master_idx (pool index, 0-based)
    name: str           # Pascal footprint name (may end in "_B")
    record_off: int     # file offset of the 8-zero signature byte
    pin_locals: List[Tuple[int, int]]  # per-pin (A, B) in Section C order


@dataclass(slots=True)
class _Chip:
    """One R3 chip record. Built by `_enumerate_r3_chips`."""
    refdes: str
    record_off: int     # offset of Pascal-length byte
    Y: int              # world Y — BL corner of chip world bbox
    X: int              # world X — BL corner of chip world bbox
    f0: int             # world Y — TR corner of chip world bbox (= Y + dy)
    f1: int             # world X — TR corner of chip world bbox (= X + dx)
    f2: int             # bbox-anchor Y (canonical pin transform origin)
    f3: int             # bbox-anchor X
    rot: int            # rotation (0 / 90 / 180 / 270)
    fp_id: int          # footprint ID, maps to master at fp_id - 1
    # uint32 at after-Pascal +32..+35. Encodes a chip-type enum used
    # by the file format to discriminate pin-layout conventions. Known
    # values on T480: 0=IC/BGA, 1=LED/Diode, 2=MOSFET, 3=Resistor,
    # 5=Capacitor, 9=Test jack, 13=Fuse, 14=Inductor, 15=Crystal,
    # 16=Switch, 17=Connector, 18=Test point, 29=Fiducial. Currently
    # only class==17 needs a non-canonical pin transform (the others
    # all resolve correctly with the canonical rule).
    chip_class: int
    # Packed Pascal-string slots starting at after-Pascal +45 (after the
    # 0x01 sentinel at +44). 100 % of T480 chips carry all 5; the agent
    # report calls these "device value / tolerance / tolerance-dup /
    # footprint name / part code". Slot 0 == "*" for ICs (placeholder),
    # actual value (e.g. "10K", "0.01") for passives.
    device_value: str   # slot 0 — passive value or "*" for ICs
    tolerance: str      # slot 1 — tolerance %
    part_code: str      # slot 4 — supplier / part code, e.g. "SD000012RYT"
    fp_name: str        # slot 3 — footprint name (== master[fp_id-1].name)
    # Per-pin records walked from the chip's inline pin list. Each tuple
    # is `(master_pin_idx, display_pin_number, pascal_pin_name)`.
    # `master_pin_idx` = (ptr - first_ptr) // 8 — selects the pin's
    # local (A, B) coord in the master record's Section C.
    # `display_pin_number` = uint32 at pin-record +8..+11 — the integer
    # BoardViewer shows in its Pin column (e.g. 148).
    # `pascal_pin_name` = the Pascal-prefixed name (e.g. "A148") —
    # kept for completeness but `display_pin_number` is what we use.
    pins: List[Tuple[int, int, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Master pool scanner
# --------------------------------------------------------------------------

def _master_candidates(data: bytes, start: int) -> List[Tuple[int, int, str]]:
    """Every `(offset, name_len, name)` from `start` to EOF whose 9-byte
    master signature is followed by a plausible Pascal ASCII name. A
    match skips past its name; anything else advances one byte."""
    raw: List[Tuple[int, int, str]] = []
    i = max(0, start)
    end = len(data) - 12
    while i < end:
        i = data.find(_MASTER_SIGNATURE, i, end + 9)
        if i < 0 or i >= end:
            break
        L = data[i+9]
        if 4 <= L <= 80:
            name_bytes = data[i+10:i+10+L]
            if all(0x20 <= b < 0x7f for b in name_bytes):
                raw.append((i, L, name_bytes.decode("ascii", errors="replace")))
                i = i + 10 + L
                continue
        i += 1
    return raw


def _scan_master_pool(data: bytes, start: int = _MASTER_POOL_START_HINT
                      ) -> Dict[int, _Master]:
    """Walk the master footprint pool starting at `start`. Returns
    ``{master_idx -> _Master}``.

    Each master record begins with the 9-byte signature
    ``00 00 00 00 00 00 00 00 01`` followed by a Pascal-prefixed
    footprint name (length 4..80, ASCII). The data after the name
    breaks into sub-sections — outline polyline (24-byte stride),
    pad-shape table, per-pin local coords (19-byte stride), and a
    bounding trailer — but for this minimum-viable parser we only
    extract the per-pin (A, B) coords from the 19-byte stride run.

    Two-pass implementation: first pass collects all valid master
    offsets+names; second pass extracts pin coords for each, using the
    NEXT master's offset as the body-end bound. The 9-byte signature
    byte sequence appears many times INSIDE master data (any run of 8
    zero bytes followed by 0x01 will match), so single-pass scanning
    with `data.find()` for the next signature picks up false positives
    and cuts master bodies far too short.
    """
    # Pass 1: collect (offset, name_len, name) for every record whose
    # 9-byte signature is followed by a plausible Pascal-prefixed
    # ASCII footprint name. This still admits some false positives
    # (e.g. a master's data containing the 9-byte pattern then a
    # coincidentally-ASCII run), but those are rare and rejected by
    # the implicit constraint that fp_id -> master_idx must yield a
    # sane footprint name for real chips.
    raw = _master_candidates(data, start)
    # Pass 2: extract pin coords with proper body bounds.
    masters: List[_Master] = []
    for k, (off, L, name) in enumerate(raw):
        body_start = off + 10 + L
        body_end = raw[k+1][0] if k + 1 < len(raw) else len(data)
        pin_locals = _extract_master_pins(data, body_start, body_end)
        masters.append(_Master(
            idx=k, name=name, record_off=off, pin_locals=pin_locals,
        ))
    return {m.idx: m for m in masters}


def _extract_master_pins(data: bytes, body_start: int, body_end: int
                         ) -> List[Tuple[int, int]]:
    """Pull Section C 19-byte per-pin records and decode each as an
    (A, B) int32 pair in master-local coordinates.

    Sections inside a master record are delimited by `ff ff ff ff`
    sentinels. Section A (outline polyline) uses 24-byte stride;
    Section C (per-pin pad coords) uses 19-byte stride. We only collect
    consecutive sentinels exactly 19 bytes apart — this skips Section A
    cleanly, skips the variable-length Section B (pad-shape table)
    between A and C, and stops at the Section D (bounding trailer)
    boundary where the stride changes again.
    """
    sentinels: List[int] = []
    pos = body_start
    while pos < body_end:
        idx = data.find(b"\xff\xff\xff\xff", pos, body_end)
        if idx < 0:
            break
        sentinels.append(idx)
        pos = idx + 4
    if not sentinels:
        return []
    pins: List[Tuple[int, int]] = []
    # Find runs of consecutive 19-spaced sentinels — each run is Section
    # C of one master. A K-pin master has K sentinels at offsets 0, 19,
    # 38, ..., 19*(K-1) from section start. The earlier pair-up loop
    # `for i in range(len(sentinels)-1)` emitted only K-1 records per
    # run (fence-post: K sentinels = K-1 pairs); we now emit ALL
    # sentinels in any run of length >= 2. Verified to give 2702/2754
    # exact match against chip-side pin counts on T480 (remaining 52
    # are test-point masters whose Section C is genuinely empty by
    # construction).
    i = 0
    while i < len(sentinels):
        run_start = i
        while i + 1 < len(sentinels) and sentinels[i + 1] - sentinels[i] == 19:
            i += 1
        if i - run_start + 1 >= 2:
            for j in range(run_start, i + 1):
                s_off = sentinels[j]
                # 19-byte record layout (from sentinel start):
                #   +0..+3   sentinel `ff ff ff ff`
                #   +4..+7   uint32 pad_shape_enum (indexes Section B)
                #   +8..+11  int32 A (local X)
                #   +12..+15 int32 B (local Y)
                #   +16..+18 3 zero padding bytes
                A = int.from_bytes(data[s_off+8:s_off+12], "little", signed=True)
                B = int.from_bytes(data[s_off+12:s_off+16], "little", signed=True)
                pins.append((A, B))
        i += 1
    return pins


# --------------------------------------------------------------------------
# Region 1 layer-flag scanner
# --------------------------------------------------------------------------

def _scan_r1_layers(data: bytes) -> Dict[str, int]:
    """Scan for Region 1 layer-flag records. Returns
    ``{refdes -> layer_byte}`` where layer_byte is the low byte of the
    int32 at after-Pascal +24..+27 (0 = TOP, 1 = BOTTOM).

    Region 1 records: any Pascal-prefixed refdes where the 8-byte
    signature ``b8 0b 00 00 2c 01 00 00`` (the chip's `0xbb800 /
    0x12c00` constants) appears at after-Pascal +12..+19. Verified on
    296 unique chip refdes in T480; never disagrees with itself across
    chips that appear in multiple regions.

    The upper 3 bytes of the layer-flag int32 occasionally carry
    non-zero metadata, so we mask to the low byte. (Across T480 the
    high bytes are 0 in 99 % of records; the rare exceptions are
    structurally consistent with low-byte-only layer encoding.)

    Dispatches to a numpy-accelerated prefilter when available; falls
    back to the pure-Python reference loop (`_scan_r1_layers_py`) on any
    error or when numpy is absent.
    """
    if _NUMPY_OK:
        try:
            return _scan_r1_layers_np(data)
        except Exception:
            pass
    return _scan_r1_layers_py(data)


def _scan_r1_layers_np(data: bytes) -> Dict[str, int]:
    """numpy fast path for `_scan_r1_layers`.

    The selective signal in the pure-Python loop is the 8-byte
    ``_R1_SIGNATURE`` that must sit at after-Pascal +12..+19, i.e. at
    file offset ``i + 1 + L + 12`` where ``i`` is the length-byte offset
    and ``L = data[i]`` the refdes length (2..16). Rather than test all
    ~12.8M positions, we locate the (few thousand) signature occurrences
    directly and recover the candidate ``i`` for each.

    For a signature at ``sig_pos``, a match position satisfies
    ``i = sig_pos - 13 - L`` with ``L = data[i]``. We try every
    ``L in [2, 16]``, keep the ones where ``data[i] == L``, then process
    the resulting candidate ``i`` values in ascending order applying the
    *identical* validation and first-seen dedup as the reference loop.
    This is exactly equivalent: the reference loop's only state is the
    ``i += 1 + L`` skip after a successful match, but that skip lands
    within the matched refdes (which ends at ``i + 1 + L``, strictly
    before the signature) and never reaches another candidate's length
    byte, so processing candidates in ascending ``i`` order with
    first-seen dedup reproduces it byte-for-byte.
    """
    n = len(data)
    if n <= 50:
        return _scan_r1_layers_py(data)
    a = _np.frombuffer(data, dtype=_np.uint8)
    sig = _np.frombuffer(_R1_SIGNATURE, dtype=_np.uint8)

    # Find signature occurrences: anchor on the first signature byte,
    # then verify the full 8-byte window vectorized.
    cand = _np.nonzero(a[: n - 8] == sig[0])[0]
    if cand.size:
        ok = _np.ones(cand.shape, dtype=bool)
        for k in range(1, 8):
            ok &= a[cand + k] == sig[k]
        sig_pos = cand[ok]
    else:
        sig_pos = cand

    # Recover candidate length-byte offsets i = sig_pos - 13 - L for
    # each plausible L, keeping those where data[i] == L. Collect into a
    # single ascending-sorted array of (i, L) pairs.
    cand_i_list = []
    for L in range(2, 17):
        ii = sig_pos - (13 + L)
        ii = ii[ii >= 0]
        if ii.size:
            ii = ii[a[ii] == L]
            if ii.size:
                cand_i_list.append(ii)
    if not cand_i_list:
        return {}
    cand_i = _np.unique(_np.concatenate(cand_i_list))  # sorted ascending

    r1: Dict[str, int] = {}
    limit = n - 50
    for i in cand_i.tolist():
        if i >= limit:
            continue
        L = data[i]  # == one of 2..16 by construction
        s = data[i + 1:i + 1 + L]
        if not all(0x20 <= b < 0x7f for b in s):
            continue
        try:
            text = s.decode("ascii")
        except UnicodeDecodeError:
            continue
        if (text[0].isalpha()
                and all(c.isalnum() or c in "_-" for c in text)
                and data[i + 1 + L + 12:i + 1 + L + 20] == _R1_SIGNATURE
                and not _KNOWN_PIN_PATTERNS.fullmatch(text)):
            if text not in r1:
                v = int.from_bytes(data[i + 1 + L + 24:i + 1 + L + 28],
                                   "little")
                r1[text] = v & 0xFF
    return r1


def _scan_r1_layers_py(data: bytes) -> Dict[str, int]:
    """Pure-Python reference implementation of `_scan_r1_layers`.

    Kept as the canonical fallback; the numpy fast path above must
    produce an identical result.
    """
    r1: Dict[str, int] = {}
    i = 0
    while i < len(data) - 50:
        L = data[i]
        if 2 <= L <= 16:
            s = data[i+1:i+1+L]
            if all(0x20 <= b < 0x7f for b in s):
                try:
                    text = s.decode("ascii")
                except UnicodeDecodeError:
                    i += 1
                    continue
                if (text[0].isalpha()
                    and all(c.isalnum() or c in "_-" for c in text)
                    and data[i+1+L+12:i+1+L+20] == _R1_SIGNATURE
                    and not _KNOWN_PIN_PATTERNS.fullmatch(text)):
                    if text not in r1:
                        v = int.from_bytes(
                            data[i+1+L+24:i+1+L+28], "little")
                        r1[text] = v & 0xFF
                    i += 1 + L
                    continue
        i += 1
    return r1


# --------------------------------------------------------------------------
# Cap-section layer overlay
# --------------------------------------------------------------------------

# Where the cap-section trailer keeps the layer byte, in preference
# order: T480 puts it at +20 (0x02 TOP / 0x0b BOTTOM) past an 11-char
# part code; T470p at +9 (0x02 TOP / 0x09 BOTTOM), like Gigabyte.
_CAP_LAYER_OFFSETS = (20, 9)


def _scan_cap_section_layers(data: bytes,
                             r1: Optional[Dict[str, int]] = None
                             ) -> Dict[str, int]:
    """For each cap-section chip record (the historical `0x01 +
    Pascal-dev + Pascal-fp` markers found by tvw_parser._chip_headers),
    return ``{refdes -> layer_byte}`` from the cap-section trailer.
    0x02 means TOP; anything else BOTTOM (see `_determine_layer`).

    The trailer offset differs between boards, so when `r1` is given
    each offset in `_CAP_LAYER_OFFSETS` is scored against the Region 1
    flags of chips found in both, and the best one wins (ties and no
    overlap keep the first, +20). On T480 all 20 overlapping chips
    agree at +20; on T470p all 323 agree at +9 while +20 is always 0.
    """
    # Re-use the existing Gigabyte chip-header finder; the marker
    # pattern is the same on both variants.
    #
    # `_chip_headers` is the accelerated wrapper: it prefers the C port
    # in tvw_native.dll and falls back to the pure-Python
    # `_find_chip_headers` when the DLL is absent, returning the same
    # list of {off, dev_name, footprint, after_off} dicts either way.
    # The native ceiling (16 K headers) is far above the ~900 a Compal
    # file produces, so there is no truncation risk on this path.
    from .parsers.tvw_parser import _chip_headers, _decode_refdes
    chips = _chip_headers(data)
    firsts: Dict[str, int] = {}
    for c in chips:
        rd = _decode_refdes(data, c["off"])
        if rd and rd not in firsts:
            firsts[rd] = c["after_off"]

    def layers_at(k: int) -> Dict[str, int]:
        return {rd: data[after + k] for rd, after in firsts.items()
                if after + k < len(data)}

    best_k, best_score = _CAP_LAYER_OFFSETS[0], -1
    if r1:
        for k in _CAP_LAYER_OFFSETS:
            lay = layers_at(k)
            score = sum(1 for rd, b in lay.items() if rd in r1
                        and (b == 0x02) == (r1[rd] == 0))
            if score > best_score:
                best_k, best_score = k, score
    return layers_at(best_k)


# --------------------------------------------------------------------------
# Region 3 chip enumerator
# --------------------------------------------------------------------------

def _enumerate_r3_chips(
    data: bytes,
    masters: Optional[Dict[int, _Master]],
    lo: int = _R3_SEARCH_START,
    hi: int = _R3_SEARCH_END,
    skip_fp_ids: frozenset = _TEST_POINT_FP_IDS,
) -> Dict[str, _Chip]:
    """Find all Region 3 chip records. Returns ``{refdes -> _Chip}``.

    Filter chain (cheapest checks first to keep the scan fast):
      * anchor must be `00 00 00 00` followed by Pascal-prefixed refdes
        with length 2..16, ASCII-only, alpha leading, alphanumeric +
        `_-` contents.
      * not a known pin sub-record pattern (`H<n>`, `FL<n>`, `FLJ<n>`).
      * byte at chip after-Pascal +44 must equal `_CHIP_PRELUDE_SENTINEL`
        (0x01) — this is true for ALL valid Compal R3 chip records
        regardless of chip type.
      * Y, X coords (after-Pascal +0..+7) must be reasonable (|val| <
        2_000_000 file units; the actual board span is ±1,000,000).
      * fp_id (after-Pascal +28..+31) must resolve to a master record
        (master_idx = fp_id - 1 in range).
      * fp_id must not be in `skip_fp_ids` (T480's test-point ids).

    ``masters=None`` skips the master lookup (fp_id must just be >= 1);
    layout autodetection uses that to collect chips before it knows
    where the master pool starts.
    """
    chips: Dict[str, _Chip] = {}
    i = max(0, lo)
    end = min(hi, len(data) - 64)
    while i < end:
        # Cheapest pre-check first: byte at i must be 0.
        if data[i] != 0:
            i += 1
            continue
        if data[i:i+4] != b"\x00\x00\x00\x00":
            i += 1
            continue
        L = data[i+4]
        if L < 2 or L > 16:
            i += 1
            continue
        s = data[i+5:i+5+L]
        if not all(0x20 <= b < 0x7f for b in s):
            i += 1
            continue
        try:
            refdes = s.decode("ascii")
        except UnicodeDecodeError:
            i += 1
            continue
        if not refdes[0].isalpha():
            i += 1
            continue
        if not all(c.isalnum() or c in "_-" for c in refdes):
            i += 1
            continue
        if _KNOWN_PIN_PATTERNS.fullmatch(refdes):
            i += 1
            continue
        # Parse preamble.
        p = i + 5 + L
        if p + 48 > len(data):
            i += 1
            continue
        if data[p+44] != _CHIP_PRELUDE_SENTINEL:
            i += 1
            continue
        Y = int.from_bytes(data[p:p+4], "little", signed=True)
        X = int.from_bytes(data[p+4:p+8], "little", signed=True)
        f0 = int.from_bytes(data[p+8:p+12], "little", signed=True)
        f1 = int.from_bytes(data[p+12:p+16], "little", signed=True)
        f2 = int.from_bytes(data[p+16:p+20], "little", signed=True)
        f3 = int.from_bytes(data[p+20:p+24], "little", signed=True)
        rot = int.from_bytes(data[p+24:p+28], "little", signed=True)
        fp_id = int.from_bytes(data[p+28:p+32], "little", signed=True)
        # uint32 chip_class at +32..+35. The 8 bytes at +36..+43 are
        # still zero in 100% of T480 records — only +32..+35 carries
        # the new field. See _Chip docstring for the enum values.
        chip_class = int.from_bytes(data[p+32:p+36], "little")
        if abs(Y) > 2_000_000 or abs(X) > 2_000_000:
            i += 1
            continue
        master_idx = fp_id - 1
        if (master_idx not in masters) if masters is not None else master_idx < 0:
            i += 1
            continue
        if fp_id in skip_fp_ids:
            i += 1
            continue
        # Walk the 5 packed Pascal strings starting at after-Pascal +45.
        # Each string: 1 length byte + L content bytes. Slots:
        #   0 = device value     (passive value, or "*" for ICs)
        #   1 = tolerance %
        #   2 = tolerance %      (duplicate of slot 1 in 100 % of cases)
        #   3 = footprint name   (= master[fp_id-1].name; verified)
        #   4 = part / supplier code
        # If any string fails to parse cleanly (bad length / non-ASCII)
        # we bail out — the record is malformed or our offsets are off.
        slots: List[str] = []
        sp = p + 45
        slots_ok = True
        for _ in range(5):
            if sp >= len(data):
                slots_ok = False
                break
            sL = data[sp]
            if sL > 100 or sp + 1 + sL > len(data):
                slots_ok = False
                break
            sb = data[sp+1:sp+1+sL]
            if not all(0x20 <= b < 0x7f for b in sb):
                slots_ok = False
                break
            slots.append(sb.decode("ascii"))
            sp += 1 + sL
        if not slots_ok:
            i += 1
            continue
        device_value, tolerance, _tol_dup, fp_name, part_code = slots
        # After slot 4: 4 zero bytes, then uint32 pin_count, uint32
        # mystery flag, 4 more bytes, then the pin-record run.
        if sp + 16 > len(data):
            i += 1
            continue
        pin_count = int.from_bytes(data[sp+4:sp+8], "little")
        pin_list_pos = sp + 16
        # Walk pin records (variable stride 17 + L per pin).
        pin_records: List[Tuple[int, int, str]] = []
        pos = pin_list_pos
        first_ptr: Optional[int] = None
        for _ in range(min(pin_count, 5000)):  # safety cap
            if pos + 18 > len(data):
                break
            ptr = int.from_bytes(data[pos:pos+4], "little")
            pin_display = int.from_bytes(data[pos+8:pos+12], "little")
            L_name = data[pos+12]
            if L_name < 1 or L_name > 12:
                break
            name_bytes = data[pos+13:pos+13+L_name]
            if not all(0x20 <= b < 0x7f for b in name_bytes):
                break
            name = name_bytes.decode("ascii")
            if first_ptr is None:
                first_ptr = ptr
                master_pin_idx = 0
            else:
                master_pin_idx = (ptr - first_ptr) // 8
            pin_records.append((master_pin_idx, pin_display, name))
            pos += 17 + L_name
        if refdes not in chips:
            chips[refdes] = _Chip(
                refdes=refdes, record_off=i + 4,
                Y=Y, X=X, f0=f0, f1=f1, f2=f2, f3=f3,
                rot=rot, fp_id=fp_id, chip_class=chip_class,
                device_value=device_value, tolerance=tolerance,
                part_code=part_code, fp_name=fp_name, pins=pin_records,
            )
        # Skip past the Pascal so we don't re-match its bytes when
        # walking forward. The chip's full data block extends further
        # (footprint name, part code, pin records) but the next anchor
        # `00 00 00 00 + Pascal-refdes` won't false-positive inside
        # that data: the embedded Pascal strings start at non-zero
        # offsets and pin records start with a uint32 ptr, not zero.
        i = i + 5 + L
    return chips


# --------------------------------------------------------------------------
# Layer determination — 3-source chain
# --------------------------------------------------------------------------

def _determine_layer(
    refdes: str,
    master_name: str,
    r1: Dict[str, int],
    cap_layers: Dict[str, int],
) -> str:
    """Return 'TOP' or 'BOTTOM' using the 3-source priority chain:

      1. Region 1 supervised flag (most reliable, 296 chips on T480).
      2. Cap-section trailer byte (covers 884 chips, mostly passives).
      3. Master `_B` suffix (covers chips with paired footprint variants).

    Chips covered by none of the three default to TOP. On T480 this
    default fires for ~10-20 truly orphan chips, all without
    electrical significance.
    """
    if refdes in r1:
        return "BOTTOM" if r1[refdes] == 1 else "TOP"
    if refdes in cap_layers:
        return "TOP" if cap_layers[refdes] == 0x02 else "BOTTOM"
    return "BOTTOM" if master_name.endswith("_B") else "TOP"


# --------------------------------------------------------------------------
# Canonical pin-position transform
# --------------------------------------------------------------------------

# Pin-frame conventions (see `_pin_local_to_world`).
_FRAME_T480 = "t480"        # canonical; connectors (class 17) special-cased
_FRAME_UNIFORM = "uniform"  # T470p: the connector frame for every chip


def _frame_negated(chip: _Chip, layer: str, frame: str) -> bool:
    """Whether (dy, dx) is negated before the BOTTOM dx-flip."""
    if frame == _FRAME_UNIFORM:
        # T470p NM-B071: verified on every asymmetric part with a full
        # pad match (SOT/QFN/SON/connectors, all 4 rotations x both
        # layers). This is also the T480 connector rule, which T480
        # never exercised for TOP-side 90/270 placements.
        return (layer == "TOP") == (chip.rot in (0, 180))
    if chip.chip_class == 17:
        # Connector pin frame matches canonical only for the two
        # "natural" cases: rot in {0, 180} on BOTTOM. In all other
        # placements (TOP-side, or rotated 90/270 on BOTTOM) the
        # frame is inverted. Empirically verified on T480:
        #   rot=0   BOTTOM   no-negate  (JTAG2, JSIM1, JWLAN1, JFAN1)
        #   rot=180 BOTTOM   no-negate  (JLCD1, JPWR1, JDDR1, JDDR2)
        #   rot=0   TOP      negate     (JTP1)
        #   rot=180 TOP      negate     (JKBL1, J41)
        #   rot=90  BOTTOM   negate     (JLAN1, JSD1, JUSB1, JHDMI1)
        #   rot=270 BOTTOM   negate     (JDOCK1, JUSBC1, JHP1, JSPK1)
        return not (chip.rot in (0, 180) and layer == "BOTTOM")
    return False


def _pin_local_to_world(
    chip: _Chip, A: int, B: int, layer: str,
    frame: str = _FRAME_T480, negate: Optional[bool] = None,
) -> Tuple[int, int]:
    """Convert master-local (A, B) to world (Y, X) for the given chip's
    rotation and layer. Returns ``(world_Y, world_X)``.

    See TVW_FORMAT.html section 11 for the derivation. Verified on
    216 supervised chips across all 4 rotations x both layers with
    residual = 0 against actual pad world coords for non-connector
    chips. Class-17 (connector) chips require the additional negation
    step in `_frame_negated`, verified on JKBL1 (38/38 pins), JTAG2
    (8/8 pins), plus pin-1 spot-checks on JLAN1, JDOCK1, JHDMI1,
    JUSBC1 and J41.

    `frame` picks the board's convention (`_choose_pin_frame`);
    `negate`, when given, is that convention's answer precomputed.
    """
    rot = chip.rot
    if   rot ==   0: dy, dx = -A, -B
    elif rot ==  90: dy, dx = -B,  A
    elif rot == 180: dy, dx =  A,  B
    elif rot == 270: dy, dx =  B, -A
    else:            dy, dx =  A,  B  # unusual rotation, accept verbatim
    if negate is None:
        negate = _frame_negated(chip, layer, frame)
    if negate:
        dy, dx = -dy, -dx
    if layer == "BOTTOM":
        dx = -dx
    return (chip.f2 + dy, chip.f3 + dx)


def _world_to_chip_local(
    chip: _Chip, world_Y: int, world_X: int,
) -> Tuple[float, float]:
    """Convert world (Y, X) back to the chip-local-as-renderer-expects
    coords stored in `Shape.pins`.

    The renderer applies `chip.X + rot(pin.dx, pin.dy)` to get pin
    world coords. We invert that here so the renderer's standard
    transform reproduces the world position we computed above.
    """
    rx = world_X - chip.X
    ry = world_Y - chip.Y
    theta_inv = math.radians(-chip.rot)
    cti, sti = math.cos(theta_inv), math.sin(theta_inv)
    dx = rx * cti - ry * sti
    dy = rx * sti + ry * cti
    return dx, dy


# --------------------------------------------------------------------------
# Net-name table
# --------------------------------------------------------------------------

NetTable = Tuple[int, int, List[str]]   # (header offset, end offset, names)


def _scan_net_names(data: bytes, lo: int = _NET_SEARCH_START,
                    hi: int = _NET_SEARCH_END) -> List[str]:
    """Locate and decode the net-name table; returns its names (or [])."""
    found = _locate_net_table(data, lo, hi)
    return found[2] if found else []


def _locate_net_table(data: bytes, lo: int = _NET_SEARCH_START,
                      hi: int = _NET_SEARCH_END) -> Optional[NetTable]:
    """Find the first net-name table whose header starts in [lo, hi).

    The table has a 2-uint32 header where the count is repeated twice,
    followed by `count` Pascal-prefixed net names. On T480 it lives at
    `0xb384fe` (count = 2774); T470p has it at `0x9048b4` (2762).

    Index `net_id` -> net name. **Net IDs are 0-based**: ``nets[0]`` is
    the first Pascal-prefixed name in the table (verified against
    ground-truth from BoardViewer.exe — GND at index 45, USBCOMP at
    1446, PECI at 1330, etc.).

    Dispatches to a numpy-accelerated prefilter when available; falls
    back to the pure-Python reference loop on any error or when numpy
    is absent. Both return the lowest-offset table that validates.
    """
    hi = min(hi, len(data) - 16)
    if hi <= lo:
        return None
    if _NUMPY_OK:
        try:
            return _locate_net_table_np(data, lo, hi)
        except Exception:
            pass
    return _locate_net_table_py(data, lo, hi)


def _walk_net_table(data: bytes, i: int) -> Optional[NetTable]:
    """Validate a candidate header at `i` (count already checked to be
    duplicated and in range) by walking every Pascal name."""
    count = int.from_bytes(data[i:i+4], "little")
    L = data[i+8]
    if not (1 <= L <= 80):
        return None
    if not all(0x20 <= b < 0x7f for b in data[i+9:i+9+L]):
        return None
    nets: List[str] = []
    pos = i + 8
    for _ in range(count):
        if pos >= len(data):
            return None
        sL = data[pos]
        if sL > 200 or pos + 1 + sL > len(data):
            return None
        sb = data[pos+1:pos+1+sL]
        if not all(0x20 <= b < 0x7f for b in sb):
            return None
        nets.append(sb.decode("ascii"))
        pos += 1 + sL
    return i, pos, nets


def _locate_net_table_np(data: bytes, lo: int, hi: int) -> Optional[NetTable]:
    """numpy prefilter: view the region as little-endian uint32 at each
    of the 4 byte alignments, so `c1 == c2` is just "element equals its
    successor". Memory stays ~(hi - lo) bytes per alignment instead of
    eight full-width uint32 copies, which matters for whole-file scans
    on phones."""
    cand: List[int] = []
    for k in range(4):
        start = lo + k
        n = (hi + 4 - start) // 4 + 1          # covers headers < hi
        if n < 2 or start + 4 * n > len(data):
            n = (len(data) - start) // 4
        if n < 2:
            continue
        u = _np.frombuffer(data, dtype="<u4", count=n, offset=start)
        c1 = u[:-1]
        m = (c1 == u[1:]) & (c1 >= 100) & (c1 <= 20000)
        offs = _np.nonzero(m)[0] * 4 + start
        cand.extend(int(o) for o in offs[offs < hi])
    for i in sorted(cand):
        found = _walk_net_table(data, i)
        if found:
            return found
    return None


def _locate_net_table_py(data: bytes, lo: int, hi: int) -> Optional[NetTable]:
    """Pure-Python reference implementation of `_locate_net_table`."""
    for i in range(lo, hi):
        c1 = int.from_bytes(data[i:i+4], "little")
        if not (100 <= c1 <= 20000):
            continue
        if c1 != int.from_bytes(data[i+4:i+8], "little"):
            continue
        found = _walk_net_table(data, i)
        if found:
            return found
    return None


# --------------------------------------------------------------------------
# Layout resolution — where the master pool, R3 chips and net table live
# --------------------------------------------------------------------------

@dataclass(slots=True)
class _Layout:
    masters: Dict[int, _Master]
    chips: Dict[str, _Chip]
    nets: List[str]
    detected: bool          # False = the T480 constants validated


def _fp_key(name: str) -> str:
    """Footprint name as R3 slot 3 spells it: bottom-side masters carry
    a `_B` suffix there that the chip record omits."""
    return name[:-2] if name.endswith("_B") else name


def _fp_agreement(chips: Dict[str, _Chip], masters: Dict[int, _Master]
                  ) -> float:
    """Fraction of chips whose slot-3 footprint name matches the master
    their fp_id selects. Chips with a `?` placeholder name don't count."""
    named = [c for c in chips.values() if c.fp_name not in ("", "?")]
    if not named:
        return 0.0
    ok = sum(1 for c in named
             if (m := masters.get(c.fp_id - 1)) is not None
             and _fp_key(m.name) == _fp_key(c.fp_name))
    return ok / len(named)


def _resolve_layout(data: bytes) -> Optional[_Layout]:
    """Use the T480 offsets when they validate; otherwise find the
    regions from the data. None when neither works."""
    masters = _scan_master_pool(data, _MASTER_POOL_START_HINT)
    legacy: Optional[_Layout] = None
    if masters:
        chips = _enumerate_r3_chips(data, masters)
        if chips:
            legacy = _Layout(masters, chips, _scan_net_names(data), False)
            if (len(chips) >= 10 and _fp_agreement(chips, masters)
                    >= _LAYOUT_MIN_FP_AGREEMENT):
                return legacy
    # Fixed offsets didn't validate: detect, but never do worse than
    # the pre-autodetect behaviour (whatever the fixed offsets found).
    return _autodetect_layout(data) or legacy


def _autodetect_layout(data: bytes) -> Optional[_Layout]:
    """Find the regions without fixed offsets.

    Observed order (T480 and T470p): net-name table, then Region 3 chip
    records, then the master footprint pool running to EOF.

      1. Locate the net table anywhere in the file.
      2. Collect R3 chip records after it without the master check
         (each carries fp_id and, in slot 3, its footprint's name).
      3. Every master-signature candidate after the table is a possible
         pool entry (R3 records themselves contain the signature, so
         there are false ones). Each chip votes for pool start index
         `j - (fp_id - 1)` for every candidate `j` named like its
         footprint; the true start collects ~every chip's vote.
      4. Rescan the pool from that start (identical to the fixed-offset
         path) and keep R3 chips that sit before it.
    """
    net = _locate_net_table(data, 0, len(data))
    after_nets = net[1] if net else 0
    loose = _enumerate_r3_chips(data, None, after_nets, len(data),
                                skip_fp_ids=frozenset())
    if not loose:
        return None
    raw = _master_candidates(data, after_nets)
    by_name: Dict[str, List[int]] = {}
    for j, (_off, _L, name) in enumerate(raw):
        by_name.setdefault(_fp_key(name), []).append(j)
    votes: Dict[int, int] = {}
    for c in loose.values():
        for j in by_name.get(_fp_key(c.fp_name), ()):
            s = j - (c.fp_id - 1)
            if s >= 0:
                votes[s] = votes.get(s, 0) + 1
    if not votes:
        return None
    start_idx = max(votes, key=lambda s: (votes[s], -s))
    if votes[start_idx] < 0.5 * len(loose):
        return None
    pool_off = raw[start_idx][0]
    masters = _scan_master_pool(data, pool_off)
    chips = _enumerate_r3_chips(data, masters, after_nets, pool_off,
                                skip_fp_ids=frozenset())
    if not chips or _fp_agreement(chips, masters) < _LAYOUT_MIN_FP_AGREEMENT:
        return None
    # T480's hard-coded test-point fp_ids don't carry over; drop the
    # same thing structurally: test points / fiducials whose master has
    # no pins (they'd only render as placeholder boxes).
    chips = {rd: c for rd, c in chips.items()
             if masters[c.fp_id - 1].pin_locals
             or c.chip_class not in _PINLESS_MARKER_CLASSES}
    return _Layout(masters, chips, net[2] if net else [], True)


# --------------------------------------------------------------------------
# Layer pad records — spatial index for pin-net matching
# --------------------------------------------------------------------------

def _build_pad_index(data: bytes) -> Dict[Tuple[int, int], int]:
    """Scan the entire file for 19-byte pad records and return
    ``{(world_Y, world_X) -> net_id}``.

    Layer pad record (per agent-confirmed Compal layout):
        +0..+1   2 zero bytes  (record sentinel)
        +2       1 byte         pad-shape enum (0=plain SMD, 1=variant,
                                2=through-hole/via, 5=BGA annular ring,
                                8=press-fit / mounting tab)
        +3..+6   uint32 net_id  (< 4000)
        +7..+10  uint32 pad_type (>= 1, < 100000)
        +11..+14 int32  Y world
        +15..+18 int32  X world

    Earlier drafts required 3 zero bytes at +0..+2 (treating the
    pad-shape byte as part of the sentinel). That hid 11,175 unique
    pad coords — primarily connector through-holes, BGA-style pads
    with annular rings, and press-fit mounting tabs — concentrated
    in the 0x008d8000..0x00914012 region (the gap between GND1 end
    and BOTTOM trace start). Without those, all class-17 connectors
    (JKBL1, JTAG2, JDOCK1, ...) returned empty pin → net mappings.

    Pads appear in every layer the net touches (1..10 per pin), all
    sharing the same `(Y, X)`. We dedup by coord, keeping the first
    seen net_id. The byte-aligned advance (`i += 1` after a match)
    is required because the dense gap-region records don't sit on a
    stride-19 grid relative to file position 0 — stride-19 advance
    happened to work historically only because of zero-padded slack
    between the early layer regions.

    Dispatches to a numpy-accelerated prefilter when available; falls
    back to the pure-Python reference loop (`_build_pad_index_py`) on
    any error or when numpy is absent.
    """
    if _NUMPY_OK:
        try:
            return _build_pad_index_np(data)
        except Exception:
            pass
    return _build_pad_index_py(data)


def _build_pad_index_np(data: bytes) -> Dict[Tuple[int, int], int]:
    """numpy fast path for `_build_pad_index`.

    The reference loop visits EVERY byte offset, always advancing by 1,
    and only does real work where the cheap pre-check (2 zero bytes at
    +0..+1) passes. We reproduce that exactly: a vectorized prefilter
    finds all 2-zero candidate offsets (~1.8M of 12.8M), then we vector-
    decode net_id / pad_type / Y / X for those offsets, apply the same
    numeric gates, and walk the survivors in ascending offset order with
    first-seen-coord dedup — identical to the reference output.
    """
    n = len(data) - 19
    if n <= 0:
        return {}
    a = _np.frombuffer(data, dtype=_np.uint8)

    # Prefilter: positions i in [0, n) with data[i]==0 and data[i+1]==0.
    base = a[:n]
    cand = _np.nonzero((base == 0) & (a[1:n + 1] == 0))[0]
    if cand.size == 0:
        return {}

    # Vector-decode the little-endian integer fields at the candidate
    # offsets. net_id (u32 @ +3), pad_type (u32 @ +7), Y (i32 @ +11),
    # X (i32 @ +15). Build each from its 4 constituent bytes so we never
    # rely on alignment.
    def _u32(off):
        return (a[cand + off].astype(_np.uint32)
                | (a[cand + off + 1].astype(_np.uint32) << 8)
                | (a[cand + off + 2].astype(_np.uint32) << 16)
                | (a[cand + off + 3].astype(_np.uint32) << 24))

    net_id = _u32(3)
    pad_type = _u32(7)
    y_u = _u32(11)
    x_u = _u32(15)
    # Signed interpretation for the coords.
    y_s = y_u.astype(_np.int64)
    y_s = _np.where(y_s >= 0x80000000, y_s - 0x100000000, y_s)
    x_s = x_u.astype(_np.int64)
    x_s = _np.where(x_s >= 0x80000000, x_s - 0x100000000, x_s)

    keep = (net_id < 4000)
    keep &= (pad_type != 0) & (pad_type < 100000)
    keep &= (y_s <= 2_000_000) & (y_s >= -2_000_000)
    keep &= (x_s <= 2_000_000) & (x_s >= -2_000_000)

    sel = _np.nonzero(keep)[0]
    if sel.size == 0:
        return {}
    # cand is already ascending, so sel preserves ascending offset order;
    # first-seen-coord dedup then matches the reference loop exactly.
    nets = net_id[sel].tolist()
    ys = y_s[sel].tolist()
    xs = x_s[sel].tolist()
    index: Dict[Tuple[int, int], int] = {}
    for Y, X, nid in zip(ys, xs, nets):
        key = (Y, X)
        if key not in index:
            index[key] = nid
    return index


def _build_pad_index_py(data: bytes) -> Dict[Tuple[int, int], int]:
    """Pure-Python reference implementation of `_build_pad_index`.

    Kept as the canonical fallback; the numpy fast path above must
    produce an identical result.
    """
    index: Dict[Tuple[int, int], int] = {}
    i = 0
    n = len(data) - 19
    while i < n:
        # Cheapest pre-check: 2 zero bytes at +0..+1. ~half the file
        # bytes are non-zero so this skips most positions quickly.
        if data[i] != 0 or data[i+1] != 0:
            i += 1
            continue
        net_id = int.from_bytes(data[i+3:i+7], "little")
        if net_id >= 4000:
            i += 1
            continue
        pad_type = int.from_bytes(data[i+7:i+11], "little")
        # pad_type==0 is the most common false-positive at this stage
        # (zero-padded alignment slack between regions). Real pad_types
        # start at 1 and stay below ~280 in the per-layer aperture
        # table; the 100k cap admits all-the-way-to-master-pad-shape
        # references (which can go higher) while rejecting random ints.
        if pad_type == 0 or pad_type >= 100000:
            i += 1
            continue
        Y = int.from_bytes(data[i+11:i+15], "little", signed=True)
        X = int.from_bytes(data[i+15:i+19], "little", signed=True)
        if abs(Y) > 2_000_000 or abs(X) > 2_000_000:
            i += 1
            continue
        key = (Y, X)
        if key not in index:
            index[key] = net_id
        # Byte-aligned advance — see docstring.
        i += 1
    return index


def _pad_hits(chip: _Chip, master: _Master, layer: str, negate: bool,
              pad_index: Dict[Tuple[int, int], int]) -> int:
    return sum(1 for A, B in master.pin_locals
               if _pin_local_to_world(chip, A, B, layer, negate=negate)
               in pad_index)


def _choose_pin_frame(chips: Dict[str, _Chip], masters: Dict[int, _Master],
                      layers: Dict[str, str],
                      pad_index: Dict[Tuple[int, int], int]) -> str:
    """The board's pin-frame convention: whichever lands more pins on
    pads. Symmetric parts score the same either way, so asymmetric ones
    decide; the T480 convention wins ties."""
    score = {_FRAME_T480: 0, _FRAME_UNIFORM: 0}
    for rd, c in chips.items():
        m = masters[c.fp_id - 1]
        if not m.pin_locals:
            continue
        for frame in score:
            neg = _frame_negated(c, layers[rd], frame)
            score[frame] += _pad_hits(c, m, layers[rd], neg, pad_index)
    return (_FRAME_UNIFORM if score[_FRAME_UNIFORM] > score[_FRAME_T480]
            else _FRAME_T480)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def parse(path: Path, data: Optional[bytes] = None) -> BoardModel:
    """Parse a Compal/Lenovo TVW file into a BoardModel.

    Produces chips with correct positions, rotations, layers,
    per-pin shape geometry from master footprints, real pin names from
    the chip's inline pin records, and **pin-net mapping via spatial
    matching against the 19-byte layer pad records**.

    Pin-net coverage: each chip pin's world coord is computed via the
    canonical transform; we look it up in a deduped pad index built
    from all 19-byte layer-pad records in the file. Exact-coord
    matching works because chip coords are integer file units and
    rotation angles are multiples of 90°, so the transform output is
    deterministic with zero residual.

    Pins whose predicted world coord doesn't match any pad are treated
    as no-connect (NC) and don't appear in `model.signals`.

    ``data`` accepts the buffer already read by the TVW variant dispatcher.
    It remains optional so direct callers retain the historical path-only API.
    """
    if data is None:
        data = Path(path).read_bytes()
    model = BoardModel()

    layout = _resolve_layout(data)
    if layout is None:
        model.warnings = [
            f"{Path(path).name}: could not locate the master footprint "
            f"pool and chip records. File may use a different Compal "
            f"sub-variant; please report on GitHub issue #1."
        ]
        return model
    masters, r3_chips, nets = layout.masters, layout.chips, layout.nets

    r1 = _scan_r1_layers(data)
    cap_layers = _scan_cap_section_layers(data, r1)
    pad_index = _build_pad_index(data)

    layers = {rd: _determine_layer(rd, masters[c.fp_id - 1].name, r1,
                                   cap_layers)
              for rd, c in r3_chips.items()}
    frame = _choose_pin_frame(r3_chips, masters, layers, pad_index)

    n_no_master_pins = 0
    n_pin_net_hits = 0
    n_pin_net_misses = 0
    for refdes, chip in r3_chips.items():
        master = masters[chip.fp_id - 1]
        layer = layers[refdes]
        negate = _frame_negated(chip, layer, frame)
        shape_name = f"_compal_{master.name}_{refdes}"
        shape = Shape(name=shape_name)

        # Build a lookup of master_pin_idx -> (display_pin_number, name)
        # so we can emit real BGA names like "A148" instead of sequential
        # integers. Master pins not present in the chip's inline list
        # (rare — usually means a chip-NC ball) fall back to a
        # sequential name based on master order.
        by_master_idx: Dict[int, Tuple[int, str]] = {
            m_idx: (pin_disp, pin_name)
            for (m_idx, pin_disp, pin_name) in chip.pins
        }

        if master.pin_locals:
            for pin_idx, (A, B) in enumerate(master.pin_locals):
                world_Y, world_X = _pin_local_to_world(
                    chip, A, B, layer, negate=negate)
                dx, dy = _world_to_chip_local(chip, world_Y, world_X)
                pin_disp, _pin_name = by_master_idx.get(
                    pin_idx, (pin_idx + 1, str(pin_idx + 1)))
                # Use the integer display number as the pin name —
                # matches what BoardViewer.exe shows in its Pin column.
                pin_name_out = str(pin_disp)
                shape.pins.append((pin_name_out, dx, dy))
                # Look up this pin's net via exact-coord match against
                # the pad index.
                net_id = pad_index.get((world_Y, world_X))
                if net_id is not None and net_id < len(nets):
                    net_name = nets[net_id]
                    if net_name:
                        model.signals.setdefault(net_name, []).append(
                            (refdes, pin_name_out))
                        n_pin_net_hits += 1
                    else:
                        n_pin_net_misses += 1
                else:
                    n_pin_net_misses += 1
            xs = [p[1] for p in shape.pins]
            ys = [p[2] for p in shape.pins]
            shape.bbox_override = (min(xs), min(ys), max(xs), max(ys))
        else:
            n_no_master_pins += 1
            shape.bbox_override = (-1000.0, -1000.0, 1000.0, 1000.0)

        model.shapes[shape_name] = shape
        # Component.device: prefer the device value from the R3 packed
        # Pascal strings (e.g. "10K", "0.1U", "1U") for passives. ICs
        # have device_value == "*" — use the master footprint name
        # instead so the user sees something meaningful in the UI.
        if chip.device_value and chip.device_value != "*":
            device_label = chip.device_value
        else:
            device_label = master.name
        comp = Component(
            refdes=refdes, x=float(chip.X), y=float(chip.Y),
            layer=layer, rotation=float(chip.rot),
            shape=shape_name,
            device=device_label,
        )
        model.components[refdes] = comp

    warns: List[str] = []
    if n_no_master_pins:
        warns.append(
            f"{n_no_master_pins} chips reference master footprints "
            f"with no pin-coordinate records (rendered as placeholder "
            f"bboxes).")
    if not nets:
        warns.append(
            "net-name table not found — net browsing will show no "
            "net names (only IDs).")
    if warns:
        model.warnings = warns
    return model


__all__ = ["parse"]
