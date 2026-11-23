"""Inspect four official 100-year national/regional river flood SHP bundles.

Safe first stage: inspect *source* attributes and geometry metadata, without
modifying raw data or assuming categorical depth codes correspond to metres.
Requires only the standard library; GeoPandas is optional for geometry QA.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import struct

DISTRICTS = {"44760": "부여군", "44790": "청양군"}
RIVER_TYPES = {"NTN": "국가하천", "RGN": "지방하천"}
EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
SHAPE_TYPES = {0: "Null", 1: "Point", 3: "PolyLine", 5: "Polygon",
               8: "MultiPoint", 11: "PointZ", 13: "PolyLineZ",
               15: "PolygonZ", 18: "MultiPointZ"}
REQUIRED_EPSG = 5186


def file_sha256(file: Path) -> str:
    h = sha256()
    with file.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inspect_dbf(file: Path, encoding: str = "utf-8") -> dict:
    """Read dBase field descriptors and every nondeleted record, no DBF package."""
    binary = file.read_bytes()
    if len(binary) < 33:
        raise ValueError(f"DBF header is too short: {file}")
    records, header_size, record_size = struct.unpack_from("<IHH", binary, 4)
    if records > 1000000 or not 33 <= header_size <= len(binary):
        raise ValueError(f"Bad DBF header/record count: {file}")
    if record_size < 1 or len(binary) < header_size + records * record_size:
        raise ValueError(f"Truncated DBF records: {file}")
    if (header_size - 33) % 32 != 0 or binary[header_size - 1] != 0x0D:
        raise ValueError(f"Unsupported DBF field-descriptor header: {file}")
    fields = []
    cursor = 1
    for at in range(32, header_size - 1, 32):
        entry = binary[at:at + 32]
        name = entry[:11].split(b"\0", 1)[0].decode("ascii", errors="replace")
        typ = chr(entry[11])
        width, precision = entry[16], entry[17]
        if not name or not width or cursor + width > record_size:
            raise ValueError(f"Invalid DBF field layout: {file}")
        fields.append({"name": name, "type": typ, "width": width,
                       "decimal_places": precision, "start": cursor})
        cursor += width
    if not fields:
        raise ValueError(f"DBF has no attribute fields: {file}")
    if cursor != record_size:
        raise ValueError(f"Unexpected DBF record length: {file}")

    observed = {f["name"]: Counter() for f in fields}
    nondeleted = 0
    for i in range(records):
        start = header_size + i * record_size
        row = binary[start:start + record_size]
        if row[0:1] == b"*":
            continue
        if row[0:1] != b" ":
            raise ValueError(f"Unknown DBF deletion marker, record {i}: {file}")
        nondeleted += 1
        for field in fields:
            begin = field["start"]
            raw = row[begin:begin + field["width"]]
            value = raw.decode(encoding, errors="strict").strip(" \x00")
            observed[field["name"]][value or "<NULL/BLANK>"] += 1
    for field in fields:
        items = observed[field["name"]].most_common()
        field["distinct_values"] = len(items)
        field["value_counts"] = [{"value": val, "count": count} for val, count in items[:30]]
        field.pop("start")
        if len(items) > 30:
            field["value_counts_truncated"] = True
    return {"record_count": records, "nondeleted_records": nondeleted,
            "fields": fields}


def inspect_shp_and_shx(shp: Path, shx: Path) -> dict:
    if shp.stat().st_size < 100 or shx.stat().st_size < 100:
        raise ValueError("Truncated shapefile")
    with shp.open("rb") as handle:
        header = handle.read(100)
    with shx.open("rb") as handle:
        index = handle.read()
    for file, data in ((shp, header), (shx, index)):
        if struct.unpack_from(">I", data, 0)[0] != 9994:
            raise ValueError(f"Invalid ESRI shape magic: {file}")
        if struct.unpack_from("<I", data, 28)[0] != 1000:
            raise ValueError(f"Unsupported ESRI shape version: {file}")
        if struct.unpack_from(">I", data, 24)[0] * 2 != file.stat().st_size:
            raise ValueError(f"Shapefile length does not match header: {file}")
    shape_type = struct.unpack_from("<I", header, 32)[0]
    index_type = struct.unpack_from("<I", index, 32)[0]
    if shape_type != index_type or shape_type not in SHAPE_TYPES:
        raise ValueError("SHP/SHX geometry type mismatch or unsupported type")
    bbox = struct.unpack_from("<4d", header, 36)
    if not all(map(lambda x: abs(x) < 1e9, bbox)):
        raise ValueError("Implausible SHP bounding box")
    if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
        raise ValueError("Reversed shapefile bounding box")
    if (len(index) - 100) % 8:
        raise ValueError("SHX entries are misaligned")
    count = (len(index) - 100) // 8
    with shp.open("rb") as handle:
        for n in range(count):
            offset_words, words = struct.unpack_from(">II", index, 100 + n * 8)
            start = offset_words * 2
            if start < 100 or start + 8 + words * 2 > shp.stat().st_size:
                raise ValueError(f"Index points outside SHP: {shp} record {n}")
            handle.seek(start)
            rec = handle.read(8)
            recnum, length = struct.unpack(">II", rec)
            if length != words or recnum != n + 1:
                raise ValueError(f"SHP/SHX record mismatch at {n}: {shp}")
    return {"shape_type_code": shape_type, "shape_type": SHAPE_TYPES[shape_type],
            "indexed_geometry_count": count, "bbox_epsg5186": list(bbox)}


def inspect_crs(prj: Path) -> dict:
    wkt = prj.read_text(encoding="utf-8-sig").strip()
    required = ("KGD2002", "Transverse_Mercator", "Central_Meridian\",127",
                "False_Easting\",200000", "False_Northing\",600000")
    compact = re.sub(r"\s+", "", wkt).lower()
    supported = all(p.lower() in compact for p in required)
    epsg = None
    try:
        from pyproj import CRS
        epsg = CRS.from_wkt(wkt).to_epsg()
    except ImportError:
        pass
    if epsg not in (None, REQUIRED_EPSG) or not supported:
        raise ValueError(f"Unexpected CRS, expected EPSG:5186: {prj}")
    return {"declared_name": "KGD2002_Central_Belt_2010",
            "epsg": REQUIRED_EPSG,
            "pyproj_epsg_detected": epsg,
            "wkt_parameters_verified": supported}


def geometry_qa_optional(shp: Path) -> dict:
    """Check polygon validity and areas when GeoPandas is installed."""
    try:
        import geopandas as gpd
    except ImportError:
        return {"status": "not_checked_geopandas_unavailable"}
    frame = gpd.read_file(shp)
    if frame.crs is None:
        raise ValueError(f"GeoPandas could not load CRS: {shp}")
    epsg = frame.crs.to_epsg()
    if epsg != REQUIRED_EPSG:
        raise ValueError(f"GeoPandas CRS mismatch: {shp}, got {epsg}")
    invalid = int((~frame.geometry.is_valid).sum())
    empties = int(frame.geometry.is_empty.sum())
    geometry_types = frame.geom_type.value_counts().to_dict()
    projected = frame.to_crs(epsg=5179)
    return {
        "status": "geometry_checked",
        "feature_count": len(frame),
        "invalid_geometries": invalid,
        "empty_geometries": empties,
        "types": {str(k): int(v) for k, v in geometry_types.items()},
        "summed_feature_area_km2_without_dissolve": round(float(projected.area.sum()) / 1e6, 6),
    }


def inspect_one(folder: Path, district: str, river: str) -> dict:
    stem = f"RFM_SGG_{river}_{district}_100"
    paths = {ext: folder / (stem + ext) for ext in EXTENSIONS}
    absent = [str(p.name) for p in paths.values() if not p.is_file()]
    if absent:
        raise FileNotFoundError(f"{stem}: missing {absent}")
    cpg = paths[".cpg"].read_text(encoding="ascii").strip().upper()
    if cpg not in {"UTF-8", "UTF8", "65001"}:
        raise ValueError(f"Unexpected DBF character encoding: {stem} {cpg}")
    dbf = inspect_dbf(paths[".dbf"])
    shapes = inspect_shp_and_shx(paths[".shp"], paths[".shx"])
    if dbf["record_count"] != shapes["indexed_geometry_count"]:
        raise ValueError(f"DBF and SHX row counts differ: {stem}")
    crs = inspect_crs(paths[".prj"])
    qa = geometry_qa_optional(paths[".shp"])
    if qa.get("feature_count") is not None and qa["feature_count"] != dbf["record_count"]:
        raise ValueError(f"GeoPandas feature count differs from DBF: {stem}")
    return {
        "stem": stem, "district": DISTRICTS[district], "river_type": RIVER_TYPES[river],
        "return_period_years": 100, "crs": crs, "attributes": dbf,
        "shape": shapes, "geometry_qa": qa,
        "source_files": [{"name": p.name, "size": p.stat().st_size,
                          "sha256": file_sha256(p)} for p in paths.values()],
        "depth_field_interpretation": "NOT_YET_CONFIRMED_CHECK_SOURCE_LEGEND",
    }


def run(input_dir: Path, output_dir: Path) -> dict:
    input_dir = input_dir.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"Will not overwrite: {output_dir}")
    if output_dir.is_relative_to(input_dir):
        raise ValueError("Output must not be inside input directory")
    observations = []
    for district in DISTRICTS:
        for river in RIVER_TYPES:
            observations.append(inspect_one(input_dir, district, river))
    report = {
        "status": "source_structure_audited_interpretation_pending",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_path": str(input_dir),
        "layers": observations,
        "recommendation": (
            "Confirm field semantics/units against original floodmap legend before "
            "assigning depth values or S1/S2 road closure thresholds."
        ),
        "no_road_network_overlay_performed": True,
        "original_files_modified": False,
    }
    lines = ["# 100년 빈도 국가·지방하천 SHP 원본 검사",
             "",
             "> 이 문서는 자동 생성된 구조·속성 보고서입니다. 침수심 코드의 물리적 의미는 원본 범례로 별도 검증해야 합니다.",
             "",
             "| 구역 | 유형 | SHP 도형 | DBF 레코드 | 좌표계 | 도형 검사 |",
             "|---|---|---:|---:|---|---|"]
    for layer in observations:
        lines.append(
            f'| {layer["district"]} | {layer["river_type"]} | '
            f'{layer["shape"]["indexed_geometry_count"]} | '
            f'{layer["attributes"]["record_count"]} | EPSG:5186 | '
            f'{layer["geometry_qa"]["status"]} |')
    for layer in observations:
        lines.extend(["", f'## {layer["stem"]}', "",
                      f'- SHP 유형: {layer["shape"]["shape_type"]}',
                      f'- 좌표계: EPSG:{layer["crs"]["epsg"]}',
                      f'- 데이터 범위(EPSG:5186): {layer["shape"]["bbox_epsg5186"]}',
                      f'- 도형 점검: {json.dumps(layer["geometry_qa"], ensure_ascii=False)}',
                      "", "| 속성명 | 자료형 | 각 속성값(건수) |",
                      "|---|---|---|"])
        for field in layer["attributes"]["fields"]:
            values = ", ".join(f'{item["value"]} ({item["count"]})'
                               for item in field["value_counts"])
            lines.append(f'| {field["name"]} | {field["type"]}'
                         f'({field["width"]}, {field["decimal_places"]}) | {values} |')
    lines.extend(["", "## 다음 단계 (아직 수행하지 않음)", "",
                  "1. 공식 범례에 따라 등급 코드와 수심 구간의 대응을 확정",
                  "2. 동일 침수구역·동일 심도 구간의 국가/지방 중복 확인",
                  "3. EPSG:5179로 안전하게 재투영, 침수심별 중첩 GIS 생성",
                  "4. 교량을 별도 검토하고 통행 차단 하한/상한 시나리오 검증"])
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "flood_shp_audit_100y.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "flood_shp_audit_100y.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.input_dir, args.output_dir)
    print(json.dumps({"status": result["status"], "layers": [
        {"name": x["stem"], "fields": [f["name"] for f in x["attributes"]["fields"]]}
        for x in result["layers"]
    ]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
