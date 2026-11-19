"""Compal TVW layout autodetection, on synthetic bytes only — real .tvw
boards are proprietary test data and never committed."""
import struct
import unittest

from src import tvw_compal as tc


def _pascal(s: str) -> bytes:
    b = s.encode("ascii")
    return bytes([len(b)]) + b


def _net_table(names):
    out = struct.pack("<II", len(names), len(names))
    return out + b"".join(_pascal(n) for n in names)


def _r3_chip(refdes, fp_id, fp_name, Y=1000, X=2000, rot=0, cls=3,
             f2=None, f3=None, npins=2):
    p = bytearray(b"\x00\x00\x00\x00" + _pascal(refdes))
    f2 = Y if f2 is None else f2
    f3 = X if f3 is None else f3
    p += struct.pack("<9i", Y, X, Y + 50, X + 50, f2, f3, rot, fp_id, cls)
    p += b"\x00" * 8 + b"\x01"           # +36..+43 zero, +44 sentinel
    for slot in ("10K", "5%", "5%", fp_name, "SD000001"):
        p += _pascal(slot)
    p += struct.pack("<IIII", 0, npins, 0, 0)
    for k in range(npins):
        p += struct.pack("<III", 0x1000 + 8 * k, 0, k + 1) + _pascal(str(k + 1))
        p += b"\x00\x00\x00\x00"
    return bytes(p)


def _master(name, pins):
    body = bytearray(tc._MASTER_SIGNATURE + _pascal(name))
    body += b"\x07" * 16                  # section A/B stand-in
    for A, B in pins:
        body += b"\xff\xff\xff\xff" + struct.pack("<Iii", 1, A, B) + b"\x00" * 3
    body += b"\x07" * 8
    return bytes(body)


def _board(fp_names, chips, pad=b"\x07" * 64):
    """net table, R3 chips (whose bytes also contain master-signature
    look-alikes), then the master pool — the T470p ordering."""
    nets = _net_table([f"NET{i}" for i in range(120)])
    r3 = b"".join(_r3_chip(*c) for c in chips)
    pool = b"".join(_master(n, [(-100, 0), (100, 0)]) for n in fp_names)
    return pad + nets + pad + r3 + pad + pool + pad


class LayoutAutodetectTest(unittest.TestCase):
    FPS = ["R_0402", "C_0402_B", "SOT23_3", "QFN16"]
    CHIPS = [("R1", 1, "R_0402"), ("C1", 2, "C_0402"),
             ("Q1", 3, "SOT23_3"), ("U1", 4, "QFN16"),
             ("R2", 1, "R_0402"), ("C2", 2, "C_0402")]

    def test_pool_found_without_fixed_offsets(self):
        data = _board(self.FPS, self.CHIPS)
        layout = tc._resolve_layout(data)
        self.assertIsNotNone(layout)
        self.assertTrue(layout.detected)
        self.assertEqual([m.name for _, m in sorted(layout.masters.items())],
                         self.FPS)
        self.assertEqual(set(layout.chips), {c[0] for c in self.CHIPS})
        self.assertEqual(len(layout.nets), 120)
        self.assertEqual(layout.masters[0].pin_locals, [(-100, 0), (100, 0)])

    def test_bottom_suffix_matches_slot_name(self):
        self.assertEqual(tc._fp_key("C_0402_B"), "C_0402")
        self.assertEqual(tc._fp_key("SOT23_3"), "SOT23_3")

    def test_unrelated_bytes_give_no_layout(self):
        self.assertIsNone(tc._resolve_layout(b"\x07" * 4096))

    def test_t480_offsets_still_win_when_they_validate(self):
        chips = self.CHIPS * 1 + [(f"R{k}", 1, "R_0402") for k in range(3, 12)]
        r3 = b"".join(_r3_chip(*c) for c in chips)
        pool = b"".join(_master(n, [(-100, 0), (100, 0)]) for n in self.FPS)
        data = bytearray(tc._MASTER_POOL_START_HINT + len(pool) + 64)
        data[tc._R3_SEARCH_START:tc._R3_SEARCH_START + len(r3)] = r3
        data[tc._MASTER_POOL_START_HINT:
             tc._MASTER_POOL_START_HINT + len(pool)] = pool
        layout = tc._resolve_layout(bytes(data))
        self.assertIsNotNone(layout)
        self.assertFalse(layout.detected)
        self.assertEqual(len(layout.chips), len({c[0] for c in chips}))


class NetTableTest(unittest.TestCase):
    def test_numpy_and_python_agree(self):
        names = [f"N{i}" for i in range(150)]
        for lead in range(4):  # every uint32 alignment
            data = (b"\x09" * (1000 + lead) + _net_table(names)
                    + b"\x09" * 50)
            py = tc._locate_net_table_py(data, 0, len(data) - 16)
            self.assertEqual(py[0], 1000 + lead)
            self.assertEqual(py[2], names)
            self.assertEqual(py[1], len(data) - 50)
            if tc._NUMPY_OK:
                self.assertEqual(
                    tc._locate_net_table_np(data, 0, len(data) - 16), py)

    def test_range_excludes_table(self):
        data = b"\x09" * 200 + _net_table([f"N{i}" for i in range(100)])
        self.assertEqual(tc._scan_net_names(data, 0, 150), [])


def _chip(rot, cls=0):
    return tc._Chip(refdes="X", record_off=0, Y=0, X=0, f0=0, f1=0, f2=0,
                    f3=0, rot=rot, fp_id=1, chip_class=cls,
                    device_value="", tolerance="", part_code="", fp_name="")


class PinFrameTest(unittest.TestCase):
    def test_t480_frame_unchanged(self):
        for rot in (0, 90, 180, 270):
            for layer in ("TOP", "BOTTOM"):
                self.assertFalse(tc._frame_negated(_chip(rot), layer, "t480"))
                self.assertEqual(
                    tc._frame_negated(_chip(rot, 17), layer, "t480"),
                    not (rot in (0, 180) and layer == "BOTTOM"))

    def test_uniform_frame(self):
        expect = {("TOP", 0): True, ("TOP", 180): True,
                  ("TOP", 90): False, ("TOP", 270): False,
                  ("BOTTOM", 0): False, ("BOTTOM", 180): False,
                  ("BOTTOM", 90): True, ("BOTTOM", 270): True}
        for (layer, rot), neg in expect.items():
            for cls in (0, 17):
                self.assertEqual(
                    tc._frame_negated(_chip(rot, cls), layer, "uniform"), neg)

    def test_frame_chosen_by_pad_hits(self):
        master = tc._Master(idx=0, name="SOT", record_off=0,
                            pin_locals=[(0, -40), (25, 40), (-25, 40)])
        chip = _chip(0)
        for frame in ("t480", "uniform"):
            neg = tc._frame_negated(chip, "TOP", frame)
            pads = {tc._pin_local_to_world(chip, A, B, "TOP", negate=neg): 1
                    for A, B in master.pin_locals}
            self.assertEqual(
                tc._choose_pin_frame({"X": chip}, {0: master},
                                     {"X": "TOP"}, pads), frame)

    def test_tie_keeps_t480(self):
        master = tc._Master(idx=0, name="R", record_off=0,
                            pin_locals=[(-10, 0), (10, 0)])
        chip = _chip(0)
        pads = {tc._pin_local_to_world(chip, A, B, "TOP"): 1
                for A, B in master.pin_locals}
        self.assertEqual(tc._choose_pin_frame({"X": chip}, {0: master},
                                              {"X": "TOP"}, pads), "t480")


if __name__ == "__main__":
    unittest.main()
