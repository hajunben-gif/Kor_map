"""Synthetic source data tests for maximum-depth classes and S1/S2 masks."""
import json
from pathlib import Path
import tempfile
import unittest

import geopandas as gpd
from shapely.geometry import MultiPolygon, box

from scripts.build_flood_depth_scenarios_100y import (
    assign_deepest_priority, generate, load_codebook, merge,
)


class FloodDepthScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.input.mkdir()
        self.codebook = Path(__file__).resolve().parents[1] / "config" / "flood_seg_code_100y.json"
        for county, shift in (("44760", 0), ("44790", 500)):
            for river, dy in (("NTN", 0), ("RGN", 6)):
                base = f"RFM_SGG_{river}_{county}_100"
                geoms = [
                    MultiPolygon([box(190000 + shift + j * 30,
                                      410000 + dy,
                                      190012 + shift + j * 30,
                                      410012 + dy)])
                    for j in range(5)
                ]
                df = gpd.GeoDataFrame({
                    "SEG_CODE": [f"N33{j}" for j in range(5)],
                    "FLDLV_FREQ": ["100"] * 5,
                    "SGG_CD": [county] * 5,
                    "geometry": geoms,
                }, crs="EPSG:5186")
                df.to_file(self.input / (base + ".shp"), index=False)
                (self.input / (base + ".cpg")).write_text("UTF-8")

    def test_class_priority_overlapping_areas(self):
        codes = [f"N33{j}" for j in range(5)]
        per_code = {
            code: [box(j * 10, 0, j * 10 + 15, 10)]
            for j, code in enumerate(codes)
        }
        priority, overall, classes, loss = assign_deepest_priority(per_code, codes)
        self.assertEqual(overall.area, 550)
        self.assertAlmostEqual(sum(x.area for x in priority.values()), overall.area)
        self.assertAlmostEqual(priority["N330"].area, 100)
        self.assertAlmostEqual(priority["N334"].area, 150)
        self.assertLessEqual(loss, 1)
        self.assertEqual(merge([priority[k] for k in codes[1:]], "s1").area, 450)

    def test_full_realistic_output_layers_and_qa(self):
        destination = self.root / "out"
        report = generate(self.input, destination, self.codebook)
        self.assertEqual(report["source_feature_count"], 20)
        self.assertEqual(report["source_crs"], "EPSG:5186")
        self.assertFalse(report["scenario_masks"]["S0"]["blocked_codes"])
        self.assertEqual(
            report["scenario_masks"]["S1"]["blocked_codes"],
            ["N331", "N332", "N333", "N334"],
        )
        self.assertLess(report["scenario_masks"]["S1"]["closure_zone_area_km2"],
                        report["scenario_masks"]["S2"]["closure_zone_area_km2"])
        self.assertTrue((destination / "flood_depth_qa.md").exists())
        gpkg = destination / "flood_100y_depth_scenarios.gpkg"
        source = gpd.read_file(gpkg, layer="source_categories")
        priority = gpd.read_file(gpkg, layer="depth_priority_5")
        s1 = gpd.read_file(gpkg, layer="scenario_s1_closure_zone")
        s2 = gpd.read_file(gpkg, layer="scenario_s2_closure_zone")
        self.assertEqual(len(source), 20)
        self.assertEqual(len(priority), 5)
        self.assertEqual(s2.crs.to_epsg(), 5186)
        self.assertAlmostEqual(s2.geometry.iloc[0].area,
                               sum(priority.geometry.area), places=4)
        self.assertLess(s1.geometry.iloc[0].area, s2.geometry.iloc[0].area)
        data = json.loads((destination / "flood_depth_qa.json").read_text("utf-8"))
        self.assertTrue(data["priority_rule"].startswith("deeper_SEG_CODE"))

    def test_bad_code_rejected(self):
        shp = self.input / "RFM_SGG_NTN_44760_100.shp"
        df = gpd.read_file(shp)
        df.loc[0, "SEG_CODE"] = "N999"
        df.to_file(shp, index=False)
        with self.assertRaisesRegex(ValueError, "expected one geometry"):
            generate(self.input, self.root / "out", self.codebook)

    def test_existing_output_rejected(self):
        out = self.root / "out"
        out.mkdir()
        with self.assertRaises(FileExistsError):
            generate(self.input, out, self.codebook)

    def test_codebook_and_scenario_validation(self):
        book, entries, sets = load_codebook(self.codebook)
        self.assertEqual(entries[-1]["code"], "N334")
        self.assertEqual(sets["S1"], tuple(f"N33{x}" for x in range(1, 5)))


if __name__ == "__main__":
    unittest.main()
