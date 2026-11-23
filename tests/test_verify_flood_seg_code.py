"""Tests for visual verification kit; synthetic GIS only, not a codebook."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon, box
from scripts.verify_flood_seg_code import create, get_reference_pngs, display_polygon


class VisualCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        for county in ("44760", "44790"):
            for river in ("NTN", "RGN"):
                stem = f"RFM_SGG_{river}_{county}_100"
                geometry = [MultiPolygon([box(190000+i*40, 410000, 190030+i*40, 410030)])
                            for i in range(5)]
                gdf = gpd.GeoDataFrame(
                    {"SEG_CODE": [f"N33{i}" for i in range(5)],
                     "FLDLV_FREQ": ["100"]*5, "SGG_CD": [county]*5,
                     "geometry": geometry}, crs="EPSG:5186")
                gdf.to_file(self.sources / (stem+".shp"), index=False)

    def test_comparison_site_has_20_features_and_unverified_depth(self):
        target = self.root / "viewer"
        result = create(self.sources, target)
        self.assertEqual(result["total_code_features"], 20)
        self.assertIsNone(result["code_to_depth_mapping"])
        self.assertEqual(len(result["image_keys_found"]), 0)
        self.assertTrue((target / "code_overlay_map.html").exists())
        self.assertTrue((target / "code_comparison.html").exists())
        csv = (target / "representative_points.csv").read_text(encoding="utf-8-sig")
        self.assertEqual(len(csv.strip().splitlines()), 21)
        html = (target / "code_comparison.html").read_text()
        self.assertIn("코드색은 임시", html)
        self.assertIn("N334", html)

    def test_png_from_zip_detected_without_interpreting_depth(self):
        archive = self.root / "images.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("official/RFM_SGG_NTN_44760_100(1).png",
                       b"\x89PNG\r\n\x1a\n" + b"fictional image bytes")
        images = get_reference_pngs(archive)
        self.assertEqual(list(images), ["NTN_44760"])
        result = create(self.sources, self.root / "viewer", archive)
        self.assertEqual(result["image_keys_found"], ["NTN_44760"])

    def test_valid_source_can_be_repaired_for_display(self):
        # A tiny invalid slit created by an illustrative reprojection effect.
        geom = Polygon([(0, 0), (10, 0), (10, 10),
                        (5, 10), (5, 9.9999), (5, 10), (0, 10)])
        self.assertFalse(geom.is_valid)
        drawn, record = display_polygon(geom, stem="synthetic", code="N333")
        self.assertTrue(drawn.is_valid)
        self.assertIsNotNone(record)
        self.assertEqual(record["code"], "N333")
        self.assertLess(record["repair_relative_area_change"], 0.0001)
        self.assertFalse(record["original_source_shp_modified"])

    def test_reject_dramatic_display_geometry_change(self):
        # A bow-tie has near zero net signed area before make_valid;
        # it cannot safely be accepted without manual review.
        bowtie = Polygon([(0, 0), (10, 10), (0, 10), (10, 0)])
        self.assertFalse(bowtie.is_valid)
        with self.assertRaisesRegex(ValueError, "alters area"):
            display_polygon(bowtie, stem="synthetic", code="N334")

    def test_raises_for_unknown_depth_code(self):
        path = self.sources / "RFM_SGG_NTN_44760_100.shp"
        df = gpd.read_file(path)
        df.loc[0, "SEG_CODE"] = "N999"
        df.to_file(path, index=False)
        with self.assertRaisesRegex(ValueError, "Unexpected depth code"):
            create(self.sources, self.root / "viewer")

    def test_avoid_overwriting_outputs(self):
        folder = self.root / "viewer"
        folder.mkdir()
        with self.assertRaises(FileExistsError):
            create(self.sources, folder)


if __name__ == "__main__":
    unittest.main()
