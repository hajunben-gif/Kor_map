"""Regression coverage for SHP/SHX/DBF parser and ZIP source staging.

Synthetic polygons only. Do NOT mistake this test for QA of actual flood data.
"""
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from scripts.audit_flood_shp_100y import inspect_dbf, inspect_shp_and_shx, inspect_one, run
from scripts.prepare_flood_source_zip import unpack_source

CRS_5186_WKT = (
    'PROJCS["KGD2002_Central_Belt_2010",'
    'GEOGCS["GCS_KGD2002",DATUM["D_Korea_Geodetic_Datum_2002",'
    'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",200000.0],'
    'PARAMETER["False_Northing",600000.0],'
    'PARAMETER["Central_Meridian",127.0],'
    'PARAMETER["Scale_Factor",1.0],'
    'PARAMETER["Latitude_Of_Origin",38.0],'
    'UNIT["Meter",1.0]]'
)


def header(shape_type: int, file_length: int, bbox: tuple[float, ...]) -> bytes:
    h = bytearray(100)
    struct.pack_into(">I", h, 0, 9994)
    struct.pack_into(">I", h, 24, file_length // 2)
    struct.pack_into("<II", h, 28, 1000, shape_type)
    struct.pack_into("<4d", h, 36, *bbox)
    return bytes(h)


def example_shape(path: Path) -> None:
    """Write 5 valid 4-point rectangular polygons to SHP + SHX."""
    shape_chunks = []
    index = []
    start = 100
    for i in range(5):
        x0, y0 = 100000.0 + i * 12, 300000.0
        x1, y1 = x0 + 10, y0 + 10
        points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        content = (struct.pack("<I4dIII", 5, x0, y0, x1, y1, 1, 5, 0)
                   + b"".join(struct.pack("<2d", *p) for p in points))
        assert len(content) % 2 == 0
        shape_chunks.append(struct.pack(">2I", i + 1, len(content) // 2) + content)
        index.append(struct.pack(">2I", start // 2, len(content) // 2))
        start += 8 + len(content)
    bbox = (100000., 300000., 100058., 300010.)
    path.with_suffix(".shp").write_bytes(header(5, start, bbox) + b"".join(shape_chunks))
    path.with_suffix(".shx").write_bytes(header(5, 100 + 8 * 5, bbox) + b"".join(index))


def example_dbf(path: Path) -> None:
    name = b"DEPTH_CLASS"
    f = bytearray(32)
    f[:len(name)] = name
    f[11] = ord("C")
    f[16] = 12
    record_count = 5
    size_of_header = 32 + 32 + 1
    record_size = 1 + 12
    h = bytearray(32)
    h[0] = 3
    struct.pack_into("<IHH", h, 4, record_count, size_of_header, record_size)
    rows = [b" " + str(i).encode().ljust(12) for i in range(1, 6)]
    path.with_suffix(".dbf").write_bytes(h + f + b"\x0d" + b"".join(rows) + b"\x1a")


def example_set(folder: Path, district: str, river: str) -> None:
    root = folder / f"RFM_SGG_{river}_{district}_100"
    example_shape(root)
    example_dbf(root)
    root.with_suffix(".prj").write_text(CRS_5186_WKT, encoding="utf-8")
    root.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")


class FloodShapefileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "input"
        self.folder.mkdir()
        for district in ("44760", "44790"):
            for river in ("RGN", "NTN"):
                example_set(self.folder, district, river)

    def test_dbf_fields_and_count(self) -> None:
        result = inspect_dbf(self.folder / "RFM_SGG_NTN_44760_100.dbf")
        self.assertEqual(result["record_count"], 5)
        self.assertEqual(result["fields"][0]["name"], "DEPTH_CLASS")
        self.assertEqual(result["fields"][0]["distinct_values"], 5)

    def test_shp_index_count_and_geometry(self) -> None:
        base = self.folder / "RFM_SGG_RGN_44790_100"
        result = inspect_shp_and_shx(base.with_suffix(".shp"), base.with_suffix(".shx"))
        self.assertEqual(result["indexed_geometry_count"], 5)
        self.assertEqual(result["shape_type"], "Polygon")

    def test_all_four_100y_source_sets(self) -> None:
        output = Path(self.temp.name) / "output"
        report = run(self.folder, output)
        self.assertEqual(len(report["layers"]), 4)
        self.assertTrue((output / "flood_shp_audit_100y.md").is_file())
        data = json.loads((output / "flood_shp_audit_100y.json").read_text())
        self.assertTrue(all(x["crs"]["epsg"] == 5186 for x in data["layers"]))

    def test_reject_missing_shx(self) -> None:
        (self.folder / "RFM_SGG_NTN_44760_100.shx").unlink()
        with self.assertRaises(FileNotFoundError):
            inspect_one(self.folder, "44760", "NTN")

    def test_reject_corrupt_dbf_record_count(self) -> None:
        path = self.folder / "RFM_SGG_RGN_44760_100.dbf"
        binary = bytearray(path.read_bytes())
        struct.pack_into("<I", binary, 4, 999)
        path.write_bytes(binary)
        with self.assertRaises(ValueError):
            inspect_one(self.folder, "44760", "RGN")

    def test_reject_incorrect_crs(self) -> None:
        path = self.folder / "RFM_SGG_RGN_44760_100.prj"
        path.write_text('GEOGCS["WGS 84"]', encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_one(self.folder, "44760", "RGN")

    def test_unzip_and_reject_duplicate_filenames(self) -> None:
        dest = Path(self.temp.name) / "unzipped"
        archive = Path(self.temp.name) / "source.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for f in self.folder.iterdir():
                z.write(f, arcname="data/" + f.name)
        unpacked = unpack_source(archive, dest)
        self.assertEqual(len(unpacked), 20)
        self.assertEqual(len(list(dest.iterdir())), 20)
        duplicates = Path(self.temp.name) / "duplicate.zip"
        with zipfile.ZipFile(duplicates, "w") as z:
            for f in self.folder.iterdir():
                z.write(f, arcname=f.name)
            z.writestr("other/RFM_SGG_RGN_44760_100.shp", b"duplicate")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            unpack_source(duplicates, Path(self.temp.name) / "bad")

    def test_allow_official_png_previews(self) -> None:
        archive = Path(self.temp.name) / "maps_included.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for f in self.folder.iterdir():
                z.write(f, arcname=f.name)
            z.writestr("RFM_SGG_NTN_44760_100.png", b"PNG preview not used for QA")
            z.writestr("previews/RFM_SGG_RGN_44790_100(1).png", b"Second preview")
        dest = Path(self.temp.name) / "maps_unpacked"
        found = unpack_source(archive, dest)
        self.assertEqual(len(found), 20)
        self.assertFalse(any(p.suffix.lower() == ".png" for p in dest.iterdir()))

    def test_reject_extra_files(self) -> None:
        archive = Path(self.temp.name) / "bad.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for f in self.folder.iterdir():
                z.write(f, arcname=f.name)
            z.writestr("not_expected.shp", b"x")
        with self.assertRaisesRegex(ValueError, "Unexpected file"):
            unpack_source(archive, Path(self.temp.name) / "output")


if __name__ == "__main__":
    unittest.main()
