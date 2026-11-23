"""Synthetic-only regression tests for source-preserving flood polygon merging."""
import json
from pathlib import Path
import tempfile
import unittest
import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon, box
from scripts.integrate_flood_100y import repair_after_reprojection
from scripts.integrate_flood_100y import integrate

class FloodIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "input"
        self.folder.mkdir()
        for district, dx in [("44760", 0), ("44790", 10)]:
            for river, dy in [("NTN", 0), ("RGN", 5)]:
                base = f"RFM_SGG_{river}_{district}_100"
                # Distinct features, some overlap across river and districts.
                polygons = [
                    MultiPolygon([box(190000 + dx + j * 30, 410000 + dy,
                                      190010 + dx + j * 30, 410010 + dy)])
                    for j in range(5)
                ]
                gdf = gpd.GeoDataFrame({
                    "SEG_CODE": [f"N33{j}" for j in range(5)],
                    "FLDLV_FREQ": ["100"] * 5,
                    "SGG_CD": [district] * 5,
                    "geometry": polygons,
                }, crs="EPSG:5186")
                gdf.to_file(self.folder / (base + ".shp"), index=False)
                # .cpg may be automatically written by GDAL, ensure it exists.
                (self.folder / (base + ".cpg")).write_text("UTF-8")

    def test_integrated_footprint_and_sources(self):
        dest = Path(self.temp.name) / "integrated"
        r = integrate(self.folder, dest)
        self.assertEqual(r["source_features"], 20)
        self.assertEqual(r["observed_seg_codes"], ["N330", "N331", "N332", "N333", "N334"])
        self.assertIsNone(r["seg_code_to_depth_metres"])
        self.assertFalse(r["scenario_road_closures_computed"])
        self.assertGreater(r["area_removed_by_union_km2"], 0)
        self.assertEqual(len(r["source_pairwise_overlaps"]), 6)
        self.assertTrue((dest / "integration_qa.md").is_file())
        gpkg = dest / "flood_100y_integrated.gpkg"
        categories = gpd.read_file(gpkg, layer="source_categories")
        footprint = gpd.read_file(gpkg, layer="union_footprint")
        self.assertEqual(len(categories), 20)
        self.assertEqual(categories.crs.to_epsg(), 5179)
        self.assertEqual(len(footprint), 1)
        self.assertTrue(footprint.geometry.is_valid.all())
        result = json.loads((dest / "integration_qa.json").read_text())
        self.assertEqual(result["status"], "integrated_flood_footprint_code_mapping_unconfirmed")

    def test_repair_microscopic_self_intersection_and_log(self):
        # Invalid bow-tie overlap with sub-millimetre displacement:
        # make_valid is expected to preserve area within 1 sq metre.
        poly = Polygon([(0, 0), (10, 0), (10, 10),
                        (5, 10), (5, 9.9999), (5, 10), (0, 10)])
        self.assertFalse(poly.is_valid)
        frame = gpd.GeoDataFrame(
            {"SEG_CODE": ["N330"], "geometry": [poly]}, crs="EPSG:5179")
        repaired, log = repair_after_reprojection(frame, "synthetic")
        self.assertTrue(repaired.geometry.is_valid.all())
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["seg_code"], "N330")
        self.assertLessEqual(log[0]["absolute_area_change_m2"],
                             log[0]["maximum_permitted_area_change_m2"])

    def test_reject_large_area_change(self):
        # Deliberate large bow-tie: repair must NOT silently alter flood area.
        poly = Polygon([(0, 0), (20, 20), (0, 20), (20, 0)])
        self.assertFalse(poly.is_valid)
        frame = gpd.GeoDataFrame(
            {"SEG_CODE": ["N331"], "geometry": [poly]}, crs="EPSG:5179")
        with self.assertRaisesRegex(ValueError, "area change too large"):
            repair_after_reprojection(frame, "synthetic")

    def test_reject_bad_code(self):
        file = self.folder / "RFM_SGG_NTN_44760_100.shp"
        frame = gpd.read_file(file)
        frame.loc[0, "SEG_CODE"] = "N999"
        frame.to_file(file, index=False)
        with self.assertRaisesRegex(ValueError, "wrong depth codes"):
            integrate(self.folder, Path(self.temp.name) / "bad-output")

    def test_do_not_overwrite(self):
        dest = Path(self.temp.name) / "exists"
        dest.mkdir()
        with self.assertRaises(FileExistsError):
            integrate(self.folder, dest)

if __name__ == "__main__":
    unittest.main()
