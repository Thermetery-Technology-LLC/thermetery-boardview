"""FZ part metadata: BOM description block + body outlines from graphics.

Synthetic Extracta/description text only — real .fz boards are
proprietary test data and never committed."""
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from src.parsers import fz_parser

EXTRACTA = "\n".join([
    "A!UNIT!mils!",
    "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!SYM_X!SYM_Y!",
    "S!LT712!1!AK_INDUCTOR_2P_433X287!NO!0!1850.00!4528.00!",
    "S!U3!1!AK_DIP8!NO!90!1000.00!1000.00!",
    "S!R9!1!AK_0402!NO!0!500.00!500.00!",
    "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!",
    "S!L6208B_PHASE4!LT712!1!1!1700.00!4528.00!!50!",
    "S!VCORE!LT712!2!2!2000.00!4528.00!!50!",
    "S!A!U3!1!1!950.00!1000.00!!20!",
    "S!B!U3!2!2!1050.00!1000.00!!20!",
    "S!A!R9!1!1!480.00!500.00!!8!",
    "S!B!R9!2!2!520.00!500.00!!8!",
    ("A!GRAPHIC_DATA_NAME!GRAPHIC_DATA_NUMBER!RECORD_TAG!GRAPHIC_DATA_1!"
     "GRAPHIC_DATA_2!GRAPHIC_DATA_3!GRAPHIC_DATA_4!GRAPHIC_DATA_5!"
     "GRAPHIC_DATA_6!GRAPHIC_DATA_7!GRAPHIC_DATA_8!GRAPHIC_DATA_9!SUBCLASS!"
     "SYM_NAME!REFDES!"),
    ("S!LINE!257!1 1!2082.01!4683.71!2082.01!4372.29!0.00!!!!!SILKSCREEN_TOP!"
     "AK_INDUCTOR_2P_433X287!LT712!"),
    ("S!RECTANGLE!259!1 2!1617.99!4372.29!2082.01!4683.71!1!!!!!"
     "PLACE_BOUND_TOP!AK_INDUCTOR_2P_433X287!LT712!"),
    # U3 is rotated 90 deg: world box 200 wide (x) x 400 tall (y) is
    # 400 x 200 in the part's local frame.
    ("S!RECTANGLE!259!2 1!900.00!800.00!1100.00!1200.00!1!!!!!"
     "PLACE_BOUND_TOP!AK_DIP8!U3!"),
    # A graphic mis-attributed to R9 spanning the board must be ignored.
    ("S!RECTANGLE!259!3 1!0.00!0.00!9000.00!9000.00!1!!!!!"
     "PLACE_BOUND_TOP!AK_0402!R9!"),
    "",
])

DESCR = "\n".join([
    "BOARD|R1.03|X|R1.03|70-XXX",
    "PARTNUMBER\tDESCRIPTION\tQTY\tLOCATION\tPARTNUMBER2",
    ("09G02X322000AK\tPWR CHOKE 0.32UH/35A SMD MAGIC/SI2C100709-R32L-R22\t1"
     "\tLT712\t\t59-XXX"),
    ("12G040200082\tIC SOCKET 8P 2.54MM DIP TOPYANG/16008-B0A0020N\t1"
     "\tU3_1\t\t70-XXX"),
    "s12G040200089AK\tIC SOCKET 8P 2.54MM DIP PINREX/980-12-308200",
    "",
])


def _fz_bytes(text: str, descr: str) -> bytes:
    content = text.encode("latin-1")
    body = struct.pack("<I", len(content)) + zlib.compress(content)
    draw = descr.encode("cp950")
    dz = zlib.compress(draw)
    block = struct.pack("<II", 0, len(draw)) + dz
    return body + block + struct.pack("<I", len(block))


class FzPartInfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "board.fz"
        path.write_bytes(_fz_bytes(EXTRACTA, DESCR))
        self.model = fz_parser.parse(path)

    def test_description_and_part_number(self):
        props = self.model.components["LT712"].props
        self.assertEqual(
            props["description"],
            "PWR CHOKE 0.32UH/35A SMD MAGIC/SI2C100709-R32L-R22")
        self.assertEqual(props["part_number"], "09G02X322000AK")

    def test_instance_suffix_and_alternates(self):
        props = self.model.components["U3"].props
        self.assertEqual(props["part_number"], "12G040200082")
        self.assertEqual(
            props["alternate 1"],
            "12G040200089AK  IC SOCKET 8P 2.54MM DIP PINREX/980-12-308200")

    def test_outline_from_place_bound(self):
        x0, y0, x1, y1 = self.model.shapes["LT712"].bbox()
        self.assertAlmostEqual(x1 - x0, 464.02, places=2)
        self.assertAlmostEqual(y1 - y0, 311.42, places=2)
        self.assertEqual(self.model.components["LT712"].props["outline"],
                         "place bound")

    def test_outline_is_in_local_frame(self):
        x0, y0, x1, y1 = self.model.shapes["U3"].bbox()
        self.assertAlmostEqual(x1 - x0, 400.0, places=3)
        self.assertAlmostEqual(y1 - y0, 200.0, places=3)

    def test_implausible_outline_ignored(self):
        shape = self.model.shapes["R9"]
        self.assertIsNone(shape.bbox_override)
        self.assertNotIn("outline", self.model.components["R9"].props)

    def test_missing_description_block_is_harmless(self):
        content = EXTRACTA.encode("latin-1")
        raw = struct.pack("<I", len(content)) + zlib.compress(content)
        path = Path(self.tmp.name) / "nodesc.fz"
        path.write_bytes(raw)
        model = fz_parser.parse(path)
        self.assertNotIn("description", model.components["LT712"].props)

    def test_cache_round_trip_keeps_description(self):
        path = Path(self.tmp.name) / "board.fz"
        key = [0] * 44
        fz_parser._cache_save(path, key=key, text="abc", descr="DESC Φ")
        self.assertEqual(fz_parser._cache_load(path, b"", key),
                         ("abc", "DESC Φ"))


class FzUnitsTest(unittest.TestCase):
    """ASUS exports declare `UNIT:millimeters`; coordinates must reach the
    canvases in mils like every other format, or the mil-tuned outline
    padding turns each passive into a >10 mm box."""

    ASUS = "\n".join([
        "UNIT:millimeters",
        "A!REFDES!COMP_INSERTION_CODE!SYM_NAME!SYM_MIRROR!SYM_ROTATE!",
        "S!PC9523!1!C0805_H57!NO!0!",
        "A!NET_NAME!REFDES!PIN_NUMBER!PIN_NAME!PIN_X!PIN_Y!TEST_POINT!RADIUS!",
        "S!+VDDC!PC9523!1!1!121.580!98.045!!0.635",
        "S!GND!PC9523!2!2!119.498!98.045!!0.635",
        "",
    ])

    def test_unit_declarations(self):
        self.assertEqual(fz_parser._unit_scale("A!UNIT!mils!\n"), 1.0)
        self.assertAlmostEqual(
            fz_parser._unit_scale("UNIT:millimeters\r\nA!REFDES!"),
            1000.0 / 25.4)
        self.assertEqual(fz_parser._unit_scale("A!REFDES!X!\n"), 1.0)

    def test_millimetre_board_normalised_to_mils(self):
        model = fz_parser._parse_extracta(self.ASUS)
        comp = model.components["PC9523"]
        self.assertAlmostEqual(comp.x, 120.539 * 1000 / 25.4, places=3)
        x0, _, x1, _ = model.shapes["PC9523"].bbox()
        self.assertAlmostEqual(x1 - x0, 2.082 * 1000 / 25.4, places=3)

    def test_asus_alternate_rows_carry_their_own_location(self):
        descr = "\n".join([
            "C671X|1.00X",
            "PARTNUMBER\tDESCRIPTION\tQTY\tLOCATION\tPARTNUMBER2",
            ("s05006-00100000\tFLASH WINBOND W25X20CLSNIG//2M SOIC-8\t3"
             "\tU2 U3 U4\tX"),
            ("05006-00100200\tFLASH GIGADEVICE GD25Q20BTIGT//2M SOP8\t3"
             "\tU2 U3 U4\tX"),
        ])
        bom = fz_parser._parse_descriptions(descr)
        self.assertEqual(bom["U3"]["part_number"], "05006-00100200")
        self.assertEqual(bom["U3"]["alternates"],
                         ["05006-00100000  FLASH WINBOND W25X20CLSNIG//2M SOIC-8"])


if __name__ == "__main__":
    unittest.main()
