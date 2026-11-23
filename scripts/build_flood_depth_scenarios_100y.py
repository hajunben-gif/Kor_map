"""Build 100-year categorized flood masks from four original EPSG:5186 SHPs.

The 5 SEG_CODE class meanings are read from config/flood_seg_code_100y.json.
If classes or national/regional river polygons overlap, prefer the deepest
available class as a CONSERVATIVE MODELING ASSUMPTION, not hydrological truth.

Outputs:
  flood_100y_depth_scenarios.gpkg
    source_categories (20 original source polygons and metadata)
    depth_priority_5 (5 mutually exclusive priority masks, polygons)
    flood_union_footprint (one combined inundation footprint)
    scenario_s1_closure_zone (all classes except N330)
    scenario_s2_closure_zone (all five classes)
  flood_depth_qa.json
  flood_depth_qa.md

No road links, bridges, or passability are inferred.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import GeometryCollection
from shapely.ops import unary_union

DISTRICTS = {"44760": "부여군", "44790": "청양군"}
RIVERS = {"NTN": "국가하천", "RGN": "지방하천"}
REQUIRED_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
CRS = "EPSG:5186"
ABS_AREA_TOL_M2 = 1.0
REL_AREA_TOL = 1e-7


def file_sha256(path):
    h = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def area_m2(geometry):
    return float(geometry.area)


def area_km2(geometry):
    return round(area_m2(geometry) / 1e6, 6)


def clean_polygon(geometry, label):
    """Fail rather than silently fixing source topology or dropping area."""
    if geometry is None or geometry.is_empty:
        return GeometryCollection()
    if not geometry.is_valid:
        raise ValueError(f"{label}: invalid polygon; run geometry QA")
    if geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"{label}: expected polygon, got {geometry.geom_type}")
    return geometry


def merge(geoms, label):
    nonempty = [g for g in geoms if g is not None and not g.is_empty]
    if not nonempty:
        return GeometryCollection()
    result = unary_union(nonempty)
    return clean_polygon(result, label)


def load_codebook(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data["codes"]
    codes = [x["code"] for x in entries]
    if codes != ["N330", "N331", "N332", "N333", "N334"]:
        raise ValueError("Unexpected codebook codes/order")
    if [(x["lower_bound_m"], x["upper_bound_m"]) for x in entries] != [
        (0, 0.5), (0.5, 1), (1, 2), (2, 5), (5, None)
    ]:
        raise ValueError("Codebook depths not as documented")
    scenarios = {x["name"]: tuple(x["blocked_seg_codes"]) for x in data["scenarios"]}
    if scenarios != {
        "S0": (), "S1": tuple(codes[1:]), "S2": tuple(codes)
    }:
        raise ValueError("Unexpected S0/S1/S2 code membership")
    return data, entries, scenarios


def load_shps(folder, codes):
    frames = []
    trace = []
    for county, name in DISTRICTS.items():
        for river, label in RIVERS.items():
            stem = f"RFM_SGG_{river}_{county}_100"
            missing = [stem + ext for ext in REQUIRED_EXTENSIONS
                       if not (folder / (stem + ext)).is_file()]
            if missing:
                raise FileNotFoundError(f"{stem}: missing {missing}")
            path = folder / f"{stem}.shp"
            frame = gpd.read_file(path)
            if frame.crs is None or frame.crs.to_epsg() != 5186:
                raise ValueError(f"{stem}: CRS must be EPSG:5186")
            expected_cols = {"SEG_CODE", "FLDLV_FREQ", "SGG_CD"}
            if not expected_cols.issubset(frame.columns):
                raise ValueError(f"{stem}: missing {expected_cols - set(frame.columns)}")
            frame = frame.copy()
            for field in expected_cols:
                frame[field] = frame[field].astype(str).str.strip()
            if len(frame) != 5 or set(frame.SEG_CODE) != set(codes):
                raise ValueError(f"{stem}: expected one geometry for each code")
            if set(frame.FLDLV_FREQ) != {"100"} or set(frame.SGG_CD) != {county}:
                raise ValueError(f"{stem}: county/frequency mismatch")
            if frame.geometry.is_empty.any() or (~frame.geometry.is_valid).any():
                raise ValueError(f"{stem}: invalid raw polygon geometry")
            if not frame.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
                raise ValueError(f"{stem}: nonpolygon geometry")
            frame = frame[["SEG_CODE", "FLDLV_FREQ", "SGG_CD", "geometry"]]
            frame["source_stem"] = stem
            frame["river_code"] = river
            frame["river_type"] = label
            frame["district"] = name
            frames.append(frame)
            trace.append({
                "stem": stem, "district": name, "river_type": label,
                "source_sha256": file_sha256(path), "features": len(frame),
                "feature_area_sum_km2": round(float(frame.geometry.area.sum()) / 1e6, 6),
            })
    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=CRS)
    if len(combined) != 20:
        raise ValueError("Expected 20 source features")
    return combined, trace


def assign_deepest_priority(per_code, codes):
    """Disjoint exclusive classes, assigning overlaps to deepest code.

    Native EPSG:5186 geometries avoid the earlier CRS reprojection invalidity.
    Returns masks, raw class union footprint, and overlap diagnostics.
    """
    unions = {code: merge(per_code[code], f"source union {code}") for code in codes}
    raw_footprint = merge(unions.values(), "all source union")
    priority = {}
    occupied = GeometryCollection()
    for code in reversed(codes):
        raw = unions[code]
        assigned = raw.difference(occupied) if not occupied.is_empty else raw
        priority[code] = clean_polygon(assigned, f"exclusive class {code}")
        occupied = merge([occupied, raw], f"priority progress {code}")
    final_footprint = merge(priority.values(), "priority masks union")
    difference_area = raw_footprint.symmetric_difference(final_footprint).area
    tol = max(ABS_AREA_TOL_M2, REL_AREA_TOL * max(raw_footprint.area, 1.0))
    if difference_area > tol:
        raise ValueError(f"Depth priority lost coverage: {difference_area:.6f}m² > {tol:.6f}m²")
    class_totals = sum(x.area for x in priority.values())
    if abs(class_totals - raw_footprint.area) > tol:
        raise ValueError("Depth classes overlap or do not cover the footprint")
    return priority, raw_footprint, unions, difference_area


def write_geo_layers(path, categories, codebook_entries, priority, footprint, scenarios):
    categories.to_file(path, layer="source_categories", driver="GPKG", index=False)
    table = []
    for entry in codebook_entries:
        code = entry["code"]
        if not priority[code].is_empty:
            table.append({
                "SEG_CODE": code,
                "depth_label_ko": entry["depth_label_ko"],
                "lower_bound_m": entry["lower_bound_m"],
                "upper_bound_m": entry["upper_bound_m"],
                "priority_rule": "deepest_code_wins",
                "geometry": priority[code],
            })
    assigned = gpd.GeoDataFrame(table, geometry="geometry", crs=CRS)
    assigned.to_file(path, layer="depth_priority_5", driver="GPKG", index=False)
    gpd.GeoDataFrame(
        {"return_period": [100], "geometry": [footprint]}, crs=CRS
    ).to_file(path, layer="flood_union_footprint", driver="GPKG", index=False)
    for name in ("S1", "S2"):
        blocked = scenarios[name]
        zone = merge([priority[code] for code in blocked], f"{name} closure mask")
        gpd.GeoDataFrame(
            {"scenario": [name], "rule": ["planar_intersection_assumption"],
             "geometry": [zone]}, crs=CRS
        ).to_file(path, layer=f"scenario_{name.lower()}_closure_zone",
                  driver="GPKG", index=False)


def generate(input_dir, output_dir, codebook_path):
    input_dir = Path(input_dir).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError(f"Output already exists; choose another directory: {output_dir}")
    if output_dir.is_relative_to(input_dir):
        raise ValueError("Output cannot be inside original SHP directory")
    codebook_path = Path(codebook_path).resolve(strict=True)
    book, entries, scenarios = load_codebook(codebook_path)
    codes = [x["code"] for x in entries]
    categories, source_trace = load_shps(input_dir, codes)
    per_code = defaultdict(list)
    for _, row in categories.iterrows():
        per_code[row.SEG_CODE].append(row.geometry)
    priority, union_footprint, class_unions, loss_m2 = assign_deepest_priority(per_code, codes)
    # Cross-class double counted area is NOT equivalent to pairwise overlap sum.
    sum_source_m2 = float(categories.geometry.area.sum())
    sum_code_union_m2 = sum(geom.area for geom in class_unions.values())
    sum_priority_m2 = sum(geom.area for geom in priority.values())
    s1mask = merge([priority[code] for code in scenarios["S1"]], "S1 mask")
    s2mask = merge([priority[code] for code in scenarios["S2"]], "S2 mask")
    tol = max(ABS_AREA_TOL_M2, REL_AREA_TOL * union_footprint.area)
    if s1mask.difference(s2mask).area > tol:
        raise ValueError("S1 must be a subset of S2")
    if union_footprint.symmetric_difference(s2mask).area > tol:
        raise ValueError("S2 must equal complete union footprint")
    if s1mask.intersection(priority["N330"]).area > tol:
        raise ValueError("S1 must not include shallow priority pixels")
    report = {
        "status": "flood_depth_masks_computed_road_overlay_pending",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "reference_url": book["source"]["url"],
        "codebook_sha256": file_sha256(codebook_path),
        "source_crs": CRS, "scenario_return_period_years": 100,
        "source_feature_count": len(categories),
        "source_files": source_trace,
        "priority_rule": "deeper_SEG_CODE_priority_for_any_overlap_conservative_assumption",
        "depth_classes": [
            {
                "code": x["code"], "depth_label_ko": x["depth_label_ko"],
                "raw_class_union_km2": area_km2(class_unions[x["code"]]),
                "exclusive_priority_area_km2": area_km2(priority[x["code"]]),
            }
            for x in entries
        ],
        "sum_original_feature_areas_km2": round(sum_source_m2 / 1e6, 6),
        "sum_per_code_union_areas_km2": round(sum_code_union_m2 / 1e6, 6),
        "sum_exclusive_priority_areas_km2": round(sum_priority_m2 / 1e6, 6),
        "union_footprint_area_km2": area_km2(union_footprint),
        "duplicate_area_removed_km2": round((sum_source_m2 - union_footprint.area) / 1e6, 6),
        "priority_coverage_symmetric_difference_m2": round(loss_m2, 6),
        "scenario_masks": {
            "S0": {"blocked_codes": [], "closure_zone_area_km2": 0.0},
            "S1": {"blocked_codes": list(scenarios["S1"]), "closure_zone_area_km2": area_km2(s1mask)},
            "S2": {"blocked_codes": list(scenarios["S2"]), "closure_zone_area_km2": area_km2(s2mask)},
        },
        "limits": [
            "This is NOT a verified simultaneous 100-year flood event.",
            "The deepest-overlap rule is an explicit conservative scenario assumption, not a physical solution.",
            "Road passability and bridge elevation have not been assessed.",
            "Source official SegCode URL covers national rivers; confirm regional river semantics independently.",
            "Flood geometry is still in source CRS EPSG:5186 to avoid reprojection-induced topology errors.",
        ],
    }
    output_dir.mkdir(parents=True)
    gpkg = output_dir / "flood_100y_depth_scenarios.gpkg"
    write_geo_layers(gpkg, categories, entries, priority, union_footprint, scenarios)
    (output_dir / "flood_depth_qa.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = [
        "# 부여·청양 100년 빈도 침수심 중첩·시나리오 GIS QA",
        "",
        "기존 SEG_CODE 대응표의 등급값을 사용했으며 중첩구역은 더 깊은 등급을 우선하는 연구상 보수적 가정.",
        "원본 좌표계 EPSG:5186 유지. 원본 SHP 미수정. 도로망 중첩 미실행.",
        "",
        f"- 원본 20개 레코드 면적 합계: {report['sum_original_feature_areas_km2']:.6f} km²",
        f"- 중복 제거한 전체 침수 범위: {report['union_footprint_area_km2']:.6f} km²",
        f"- S1(>=0.5m 등급) 차단구역: {report['scenario_masks']['S1']['closure_zone_area_km2']:.6f} km²",
        f"- S2(모든 침수구역) 차단구역: {report['scenario_masks']['S2']['closure_zone_area_km2']:.6f} km²",
        "",
        "| 코드 | 침수심 | 같은 등급의 합집합 km² | 가장 깊은 등급 우선 후 km² |",
        "|---|---|---:|---:|",
    ]
    for x in report["depth_classes"]:
        md.append(f"| {x['code']} | {x['depth_label_ko']} | {x['raw_class_union_km2']:.6f} | {x['exclusive_priority_area_km2']:.6f} |")
    md += [
        "", "## GPKG 레이어",
        "- source_categories: 출처와 원본 코드 20개 보존",
        "- depth_priority_5: 중첩구역 수심 등급 우선순위 적용 후 배타적 범위",
        "- flood_union_footprint: 중복 제거한 전체 범람구역",
        "- scenario_s1_closure_zone: N331~N334 기준",
        "- scenario_s2_closure_zone: N330~N334 기준",
        "",
        "공식 코드 출처: " + book["source"]["url"],
        "",
        "주의: S1/S2는 도로망 교차 분석에 투입할 **면적 마스크**일 뿐, 아직 차단 도로의 목록이나 현장 통행 안전 여부를 뜻하지 않음.",
    ]
    (output_dir / "flood_depth_qa.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--codebook", type=Path,
                        default=Path(__file__).resolve().parents[1] / "config" / "flood_seg_code_100y.json")
    args = parser.parse_args()
    report = generate(args.input_dir, args.output_dir, args.codebook)
    print(json.dumps({
        "status": report["status"], "union_area_km2": report["union_footprint_area_km2"],
        "S1_km2": report["scenario_masks"]["S1"]["closure_zone_area_km2"],
        "S2_km2": report["scenario_masks"]["S2"]["closure_zone_area_km2"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
