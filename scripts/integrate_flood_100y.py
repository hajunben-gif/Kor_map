"""Build a 100-year flood footprint and provenance-preserving GeoPackage.

No SEG_CODE => physical depth mapping is made; S0-S2 roads are not classified.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

DISTRICTS = {"44760": "부여군", "44790": "청양군"}
RIVERS = {"NTN": "국가하천", "RGN": "지방하천"}
CODES = {"N330", "N331", "N332", "N333", "N334"}
EXTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")


def checksum(path):
    h = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def checked_union(geometries):
    result = unary_union(list(geometries))
    if result.is_empty or not result.is_valid:
        raise ValueError("Empty or invalid polygon union: manual QA needed")
    return result


def km2(geometry):
    return round(float(geometry.area) / 1e6, 6)


def load_source(folder, district, river):
    stem = f"RFM_SGG_{river}_{district}_100"
    missing = [stem + x for x in EXTS if not (folder / (stem + x)).is_file()]
    if missing:
        raise FileNotFoundError(f"{stem}: missing {missing}")
    frame = gpd.read_file(folder / (stem + ".shp"))
    if frame.crs is None or frame.crs.to_epsg() != 5186:
        raise ValueError(f"{stem}: unexpected CRS {frame.crs}")
    if len(frame) != 5 or frame.geometry.is_empty.any() or (~frame.geometry.is_valid).any():
        raise ValueError(f"{stem}: unexpected count or invalid geometry")
    for attr in ("SEG_CODE", "FLDLV_FREQ", "SGG_CD"):
        if attr not in frame.columns:
            raise ValueError(f"{stem}: missing {attr}")
        frame[attr] = frame[attr].astype(str).str.strip()
    codes = frame["SEG_CODE"].tolist()
    if len(set(codes)) != 5 or set(codes) != CODES:
        raise ValueError(f"{stem}: wrong depth codes {codes}")
    if set(frame["FLDLV_FREQ"]) != {"100"} or set(frame["SGG_CD"]) != {district}:
        raise ValueError(f"{stem}: frequency/district mismatch")
    frame = frame.to_crs(epsg=5179)
    if frame.geometry.is_empty.any() or (~frame.geometry.is_valid).any():
        raise ValueError(f"{stem}: invalid after reprojection")
    frame = frame[["SEG_CODE", "FLDLV_FREQ", "SGG_CD", "geometry"]].copy()
    frame["source_stem"] = stem
    frame["river_code"] = river
    frame["river_type"] = RIVERS[river]
    frame["district"] = DISTRICTS[district]
    union = checked_union(frame.geometry)
    record = {
        "stem": stem, "river_type": RIVERS[river], "district": DISTRICTS[district],
        "features": len(frame), "codes": sorted(codes),
        "raw_shp_sha256": checksum(folder / (stem + ".shp")),
        "area_sum_km2": round(float(frame.geometry.area.sum()) / 1e6, 6),
        "dissolved_area_km2": km2(union),
    }
    return frame, union, record


def integrate(input_dir, output_dir):
    input_dir = Path(input_dir).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError(f"Will not overwrite: {output_dir}")
    if output_dir.is_relative_to(input_dir):
        raise ValueError("Output directory cannot be inside source directory")
    frames, unions, entries = [], {}, []
    for district in DISTRICTS:
        for river in RIVERS:
            frame, dissolved, record = load_source(input_dir, district, river)
            frames.append(frame)
            unions[record["stem"]] = dissolved
            entries.append(record)
    categories = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True),
                                   geometry="geometry", crs="EPSG:5179")
    footprint = checked_union(unions.values())
    # No depth assigned to dissolved area: source categories remain separate.
    footprints = gpd.GeoDataFrame({"return_period_years": [100],
                                   "geometry": [footprint]}, crs="EPSG:5179")
    comparisons = []
    stems = list(unions)
    for i, a in enumerate(stems):
        for b in stems[i + 1:]:
            overlay = unions[a].intersection(unions[b])
            comparisons.append({"a": a, "b": b, "overlap_km2": km2(overlay)})
    summed = round(float(categories.geometry.area.sum()) / 1e6, 6)
    report = {
        "status": "integrated_flood_footprint_code_mapping_unconfirmed",
        "utc": datetime.now(timezone.utc).isoformat(),
        "crs": "EPSG:5179", "source_crs": "EPSG:5186",
        "return_period_years": 100,
        "source_features": len(categories),
        "sources": entries,
        "observed_seg_codes": sorted(CODES),
        "seg_code_to_depth_metres": None,
        "scenario_road_closures_computed": False,
        "summed_source_area_km2": summed,
        "union_area_km2": km2(footprint),
        "area_removed_by_union_km2": round(summed - km2(footprint), 6),
        "source_pairwise_overlaps": comparisons,
        "notes": [
            "Do not sum pairwise overlaps as total overlap; intersections may overlap.",
            "Union footprint contains no depth class, because code meanings are not verified.",
            "Source categories remain separable in GeoPackage layer source_categories.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    gpkg = output_dir / "flood_100y_integrated.gpkg"
    categories.to_file(gpkg, layer="source_categories", driver="GPKG", index=False)
    footprints.to_file(gpkg, layer="union_footprint", driver="GPKG", index=False)
    (output_dir / "integration_qa.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# 부여·청양 100년 빈도 침수범위 통합 결과",
        "",
        "수심 코드 N330~N334 의미 미확정. 도로 차단 분석 이전의 범위 검증 산출물.",
        "",
        "| 원본 | 피처 | 면적 합 km² | 원본 합집합 km² |",
        "|---|---:|---:|---:|",
    ]
    for x in entries:
        lines.append(f'| {x["stem"]} | {x["features"]} | {x["area_sum_km2"]} | {x["dissolved_area_km2"]} |')
    lines += [
        "",
        f'- 합집합 전 총면적: {summed} km²',
        f'- 전체 범람구역 중복 제거 면적: {report["union_area_km2"]} km²',
        f'- 중복 제거 차이: {report["area_removed_by_union_km2"]} km²',
        "",
        "## 국가·지방/행정구역 원본 간 중첩 면적",
        "",
        "| A | B | 중첩 km² |",
        "|---|---|---:|",
    ]
    for x in comparisons:
        lines.append(f'| {x["a"]} | {x["b"]} | {x["overlap_km2"]} |')
    lines += [
        "",
        "GeoPackage: source_categories (원본 코드/출처 보존, 총 20 피처), union_footprint (수심정보 없는 통합 범람범위).",
        "",
        "미진행: 공식 코드표 검증, 중첩 지점의 침수심 해석, 교량 검토, 도로별 통행 시나리오.",
    ]
    (output_dir / "integration_qa.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    r = integrate(a.input_dir, a.output_dir)
    print(json.dumps({"status": r["status"], "source_features": r["source_features"],
                      "union_area_km2": r["union_area_km2"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
