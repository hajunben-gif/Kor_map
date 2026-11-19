"""Rebuild the single 2 km driving network from a verified PBF and compare facilities.

Run from the repository root with python -m scripts.build_integrated_road_network.
Use --replay-jukrim for an offline check of the preserved local source evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from importlib.metadata import version
import json
import platform
from pathlib import Path
import re
import shlex
import tempfile

import geopandas as gpd
import numpy as np
import osmnx as ox
from pyproj import CRS
import shapely
from shapely.geometry import Point, shape

from scripts import analyze_road_access as access
from scripts.build_integrated_area import load_boundary, valid_polygon
from scripts.integrated_pbf_source import acquire_pbf
from scripts.integrated_road_graph import (
    CONTROL_NODES, DRIVE, TARGETS, WAY_AUDIT_FIELDS, audit_source_ways, build_graphs,
    drive_rejections, json_text, jukrim_links, metric_line, read_pbf, strong_index,
)
from scripts.jukrim_pbf_audit import file_hash, pbf_header, write_json

ROOT = Path(__file__).resolve().parents[1]
AREA = Path("output/integrated_area/boundaries_20261113/buffer_2000m_epsg5179.geojson")
REFERENCE = Path("output/road_network_audit/jukrim/verified_20261112/pbf_results/evidence.json")
OLD_CSV = Path("data/processed/shelters_road_access_147.csv")
OLD_VALUES = ("road_distance_m", "road_edge_osmid", "road_edge_highway", "road_edge_u",
              "road_edge_v", "road_edge_key", "road_wcc_id", "road_wcc_is_largest",
              "road_access_status")
REVIEW = {
    "177": "진입도로·교량·지천로 원본 노드와 방향별 도달성은 jukrim.json 참조. 차량 통행 현장 확인 필요.",
    "140": "경계 바깥 최근접점 및 기존 그래프의 way 포함 여부로 범위 영향을 점검. 과거 절단 단계별 재현 자료 필요.",
    "125": "Kakao 건물명 공란. 시설 위치 독립 확인과 도로 형상·필터 대조가 각각 필요.",
    "160": "시설에서 기존 거리 이내는 기존 군 경계 내부. 가까운 제외 way와 독립 도로자료 대조 필요.",
    "150": "가까운 제외 way와 제외 태그를 확인. OSM에 없는 도로는 독립 자료로 확인 필요.",
    "145": "기존 말단 노드의 원본 공유 way와 방향 정보 확인. 현장 진입 가능 여부 미확인.",
}
OUTPUT_COLUMNS = [
    *(f"old_{value}" for value in OLD_VALUES), *access.NEW_COLUMNS,
    "new_u_scc_id", "new_u_scc_nodes", "new_v_scc_id", "new_v_scc_nodes",
    "new_edge_return_possible", "old_gt_300m", "new_gt_300m", "outlier_300m_status",
    "distance_change_m", "distance_reduction_pct", "distance_to_outer_boundary_m",
    "nearest_search_covered",
]
DETAIL_COLUMNS = [
    "시군명", "시설일련번호", "시설명", "latitude", "longitude", *OUTPUT_COLUMNS,
    "county_boundary_distance_m", "nearest_point_outside_old_county",
    "new_nearest_way_in_old_county_graph", "nearest_non_drive_way",
    "nearest_non_drive_highway", "nearest_non_drive_distance_m", "nearest_non_drive_filter",
    "old_terminus_osm_check", "remaining_review",
]


def load_area(root):
    raw = json.loads((root / AREA).read_bytes())
    if CRS.from_user_input(raw["crs"]["properties"]["name"]).to_epsg() != 5179:
        raise ValueError("The supplied 2 km polygon must use EPSG:5179")
    features = raw["features"]
    if len(features) != 1 or features[0]["properties"]["scope"] != "buffer_2000m":
        raise ValueError("Expected the validated 2 km buffer")
    return valid_polygon(shape(features[0]["geometry"]), "2 km buffer")


def county_paths(root, name):
    folder = root / access.NETWORK_ROOT / access.REGIONS[name]
    return folder / "boundary.geojson", folder / "network.graphml", folder / "metadata.json"


def load_counties(root):
    return {name: load_boundary(county_paths(root, name)[0], name)[0] for name in access.REGIONS}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader), reader.fieldnames


def load_facilities(root):
    original, columns = read_csv(root / access.CSV_PATH)
    previous, _ = read_csv(root / OLD_CSV)
    current_ids = {access.identity(row) for row in original}
    prior = {access.identity(row): row for row in previous}
    if len(original) != len(previous) or len(original) != len(current_ids) or current_ids != prior.keys():
        raise ValueError("Facility composite keys differ between source CSVs")
    if any(any(prior[access.identity(row)][field] != row[field] for field in columns) for row in original):
        raise ValueError("Original facility data differ from the baseline CSV")
    excluded = [row for row in original if not (row["latitude"] or row["longitude"])]
    if (len(original) != 147 or len(excluded) != 1 or excluded[0]["시설명"] != "대동마을회관"
            or excluded[0]["geocoding_status"] != "location_unverified"):
        raise ValueError("Expected 146 coordinate holders and the unlocated Daedong facility")
    if any(prior[access.identity(row)]["road_access_status"] !=
           ("excluded_missing_coordinates" if row in excluded else "matched") for row in original):
        raise ValueError("Existing facility access status differs from the baseline")
    old_outliers = {access.identity(row) for row in previous if over(row["road_distance_m"], 300)}
    if old_outliers != {("청양군", number) for number in REVIEW}:
        raise ValueError("Existing 300 m outlier population differs from documented cases")
    return original, columns, prior


def over(value, limit):
    return bool(value) and float(value) > limit


def snapshot_metadata(path, reference, checksum, source, download_metadata=None):
    if ox.__version__ != "2.1.1":
        raise ValueError("Expected OSMnx 2.1.1")
    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".pbf":
        raise ValueError("A local .pbf file is required")
    checksum = checksum or reference["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Expected a 64-character lowercase SHA-256")
    actual = file_hash(path)
    if actual != checksum:
        raise ValueError("PBF SHA-256 mismatch; for a different snapshot provide its verified SHA-256")
    header = pbf_header(path)
    exact = actual == reference["sha256"]
    if exact and (header["data_timestamp"] != reference["data_timestamp"]
                  or path.stat().st_size != reference["bytes"]):
        raise ValueError("Historical PBF header or size does not match the preserved evidence")
    if not exact and not source:
        raise ValueError("A different snapshot requires a named upstream --source")
    local_verification = "archived SHA-256, size and timestamp" if exact else "supplied SHA-256 and PBF header"
    return dict(
        **header, path=str(path), sha256=actual, bytes=path.stat().st_size,
        same_as_archived_pbf=exact, upstream=source or reference["source_origin"],
        verification=download_metadata["verification"] if download_metadata else local_verification,
        path_lifecycle="temporary download removed after execution" if download_metadata else "input file retained",
        download_status="verified" if download_metadata else "local file, download not performed",
        download_metadata=download_metadata, license="OpenStreetMap contributors / ODbL 1.0",
        reference_data_timestamp=reference["data_timestamp"], reference_sha256=reference["sha256"],
    )


def spatial_index(graph):
    zero = [(u, v, k) for u, v, k, d in graph.edges(keys=True, data=True) if d["length_5179"] == 0]
    if not zero:
        return access.RoadIndex(graph, "integrated")
    # Source topology and traces retain zero-length segments between distinct
    # OSM nodes. Distance is defined over road lines with positive length.
    spatial = graph.copy()
    spatial.remove_edges_from(zero)
    index = access.RoadIndex(spatial, "integrated")
    index.node_wcc, index.components = access.wcc_index(graph, "integrated")
    return index


def compare_facilities(original, prior, raw, graph, area):
    raw_index, index = spatial_index(raw), spatial_index(graph)
    scc = strong_index(graph)
    networks = dict.fromkeys(access.REGIONS, index)
    results = []
    for row in original:
        result = access.analyze_row(row, networks)
        old = prior[access.identity(row)]
        result.update({f"old_{key}": old[key] for key in OLD_VALUES})
        result.update({key: "" for key in OUTPUT_COLUMNS if key not in result})
        if result["road_access_status"] != "matched":
            results.append(result)
            continue
        px, py = access.TO_METRES.transform(float(row["longitude"]), float(row["latitude"]), errcheck=True)
        point = Point(px, py)
        new, previous = float(result["road_distance_m"]), float(old["road_distance_m"])
        boundary_distance = point.distance(area.boundary)
        raw_match = raw_index.nearest(point)
        if abs(new - raw_match["distance"]) > 1e-6:
            raise ValueError(f"Raw/simplified distance mismatch for {access.identity(row)}")
        if not area.covers(point) or new >= boundary_distance - 1e-6:
            raise ValueError(f"Nearest search may need a wider analysis area for {access.identity(row)}")
        u, v = int(result["road_edge_u"]), int(result["road_edge_v"])
        old_out, new_out = previous > 300, new > 300
        status = {(False, False): "below_threshold", (True, False): "resolved",
                  (False, True): "new", (True, True): "remaining"}[old_out, new_out]
        result.update(
            new_u_scc_id=scc[u][0], new_u_scc_nodes=scc[u][1],
            new_v_scc_id=scc[v][0], new_v_scc_nodes=scc[v][1],
            new_edge_return_possible=scc[u][0] == scc[v][0],
            old_gt_300m=old_out, new_gt_300m=new_out, outlier_300m_status=status,
            distance_change_m=f"{new - previous:.6f}",
            distance_reduction_pct=f"{100 * (previous - new) / previous:.6f}" if previous else "",
            distance_to_outer_boundary_m=f"{boundary_distance:.6f}", nearest_search_covered=True,
        )
        results.append(result)
    return results, index


def old_way_ids(graph):
    ids = set()
    for _, _, data in graph.edges(data=True):
        values = data["osmid"]
        ids.update(values if isinstance(values, list) else [values])
    return ids


def detailed_outliers(results, counties, old_graph, roads, rejected, graph, index):
    ids = old_way_ids(old_graph)
    excluded = {wid: metric_line(road["coords"]) for wid, road in roads.items() if rejected[wid]}
    outliers = []
    for row in results:
        county, number = access.identity(row)
        if county != "청양군" or number not in REVIEW:
            continue
        point = Point(float(row["road_facility_x_5179"]), float(row["road_facility_y_5179"]))
        edge_point = Point(float(row["road_nearest_x_5179"]), float(row["road_nearest_y_5179"]))
        exhaustive = float(np.min(shapely.distance(index.lines, point)))
        if abs(exhaustive - float(row["road_distance_m"])) > 2e-6:
            raise ValueError(f"Exhaustive distance validation failed for {number}")
        way = int(row["road_edge_osmid"])
        details = dict(
            county_boundary_distance_m=round(point.distance(counties[county].boundary), 6),
            nearest_point_outside_old_county=not counties[county].covers(edge_point),
            new_nearest_way_in_old_county_graph=way in ids, remaining_review=REVIEW[number],
            old_terminus_osm_check="", nearest_non_drive_way="", nearest_non_drive_highway="",
            nearest_non_drive_distance_m="", nearest_non_drive_filter="",
        )
        if excluded:
            alternative = min(excluded, key=lambda wid: (excluded[wid].distance(point), wid))
            details.update(
                nearest_non_drive_way=alternative, nearest_non_drive_highway=roads[alternative]["tags"]["highway"],
                nearest_non_drive_distance_m=round(point.distance(excluded[alternative]), 6),
                nearest_non_drive_filter=json_text(rejected[alternative]),
            )
        if number == "145":
            endpoints = [int(row[key]) for key in ("old_road_edge_u", "old_road_edge_v")]
            termini = [n for n in endpoints if len(set(old_graph.predecessors(n)) | set(old_graph.successors(n))) == 1]
            details["old_terminus_osm_check"] = json_text({
                node: {
                    "source_ways": {wid: rejected[wid] for wid, road in roads.items() if node in road["nodes"]},
                    "raw_predecessors": sorted(graph.predecessors(node)) if node in graph else [],
                    "raw_successors": sorted(graph.successors(node)) if node in graph else [],
                } for node in termini
            })
        outliers.append({**{field: row[field] for field in DETAIL_COLUMNS if field in row}, **details})
    return outliers


def summarize(rows, prior):
    matched = [row for row in rows if row["road_access_status"] == "matched"]
    statuses = Counter(row["outlier_300m_status"] for row in matched)
    changes = [float(row["distance_change_m"]) for row in matched]
    return dict(
        total_facilities=len(rows), analyzed_facilities=len(rows) - 1, matched_facilities=len(matched),
        excluded_missing_coordinates=sum(row["road_access_status"].startswith("excluded_") for row in rows),
        failed_facilities=sum(row["road_access_status"].startswith("failed_") for row in rows),
        resolved_300m=statuses["resolved"], remaining_300m=statuses["remaining"], new_300m=statuses["new"],
        distance_increased=sum(value > 0 for value in changes), distance_decreased=sum(value < 0 for value in changes),
        distance_equal=sum(value == 0 for value in changes),
        old_distance=access.distance_stats(list(prior.values())), new_distance=access.distance_stats(matched),
        thresholds_m={limit: {"before": sum(over(row["road_distance_m"], limit) for row in prior.values()),
                              "after": sum(over(row["road_distance_m"], limit) for row in matched)}
                      for limit in (50, 100, 300)},
    )


def review_jukrim(raw, simplified, row, evidence, exact, scope):
    nearest = dict(distance_m=float(row["road_distance_m"]), way_id=int(row["road_edge_osmid"]))
    before = evidence["nearest"]["drive"]
    stages = {"raw": jukrim_links(raw), "simplified": jukrim_links(simplified)}
    original, reduced = stages.values()

    def is_connected(stage):
        return (all(stage["edges_by_way"].values())
                and CONTROL_NODES[1] in stage["access_bridge_shared_osm_nodes"]
                and CONTROL_NODES[2] in stage["bridge_trunk_shared_osm_nodes"]
                and all(stage["directed_reachability"].values())
                and all(stage["trunk_contact_movements"].values()))

    confirmed = all(is_connected(stage) for stage in stages.values())
    difference = nearest["distance_m"] - before["distance_m"]
    if (original["directed_reachability"] != reduced["directed_reachability"]
            or {key: len(edges) for key, edges in original["trunk_contact_movements"].items()} !=
            {key: len(edges) for key, edges in reduced["trunk_contact_movements"].items()}):
        raise ValueError("Jukrim directional links changed in simplification")
    if exact and (not confirmed or abs(difference) > 0.001 or nearest["way_id"] != TARGETS[0]):
        raise ValueError("Verified Jukrim reference distance or directed connections did not reproduce")
    return dict(
        scope=scope, old_county_distance_m=float(row["old_road_distance_m"]),
        old_buyeo_comparison_m=259.8930035442013,
        preserved_pbf_reference_m=before["distance_m"], compared_to_same_snapshot=exact,
        new_nearest=nearest, new_minus_preserved_distance_m=difference, stages=stages,
        reference_omission_resolved_in_tested_scope=confirmed and nearest["way_id"] == TARGETS[0],
        source_directed_geometry_preserved=True,
        interpretation="OSM node and directional connectivity only; field vehicle access is unverified.",
    )


def save_csv(path, fields, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def saved_tags_match(saved, expected):
    if isinstance(saved, dict):
        saved = json_text(saved)
    return saved == expected


def export_graphs(destination, raw, simplified):
    gpkg = destination / "network.gpkg"
    for label, graph in (("raw", raw), ("simplified", simplified)):
        graphml = destination / f"{label}.graphml"
        ox.save_graphml(graph, graphml)
        loaded = ox.load_graphml(graphml, edge_dtypes={"raw_segments": int, "length_5179": float})
        edges = set(graph.edges(keys=True))
        if set(loaded.nodes) != set(graph.nodes) or set(loaded.edges(keys=True)) != edges:
            raise ValueError(f"{label} GraphML vertex or edge IDs changed")
        if loaded.graph.get("crs") != graph.graph["crs"] or any(
            (loaded.nodes[node]["x"], loaded.nodes[node]["y"]) != (data["x"], data["y"])
            or not saved_tags_match(loaded.nodes[node].get("osm_tags"), data.get("osm_tags"))
            for node, data in graph.nodes(data=True)
        ):
            raise ValueError(f"{label} GraphML source nodes or CRS changed")
        for edge in edges:
            actual, expected = loaded.edges[edge], graph.edges[edge]
            fields = ("osmid", "raw_segments", "geometry", "oneway", "reversed", "length_5179")
            if (not saved_tags_match(actual["osm_tags"], expected["osm_tags"])
                    or any(actual[field] != expected[field] for field in fields)):
                raise ValueError(f"{label} GraphML attributes changed for {edge}")
        nodes, lines = ox.graph_to_gdfs(graph)
        lines["raw_segments"] = lines["raw_segments"].map(json_text)
        for name, frame, keys in (("nodes", nodes, ["osmid"]), ("edges", lines, ["u", "v", "key"])):
            layer = f"{label}_{name}"
            frame.reset_index().to_file(gpkg, layer=layer, driver="GPKG", engine="pyogrio", index=False)
            restored = gpd.read_file(gpkg, layer=layer)
            restored_ids = list(restored[keys].itertuples(index=False, name=None))
            expected_ids = list(frame.reset_index()[keys].itertuples(index=False, name=None))
            if restored.crs.to_epsg() != 4326 or restored_ids != expected_ids or not restored.geometry.equals(
                frame.geometry.reset_index(drop=True)
            ):
                raise ValueError(f"{layer} GeoPackage CRS, coordinates or identifiers changed")
            for field in ("osmid", "raw_segments", "osm_tags"):
                if field in frame and restored[field].fillna("").tolist() != frame[field].fillna("").tolist():
                    raise ValueError(f"{layer} GeoPackage changed field {field}")


def render_report(destination, meta, outliers, jukrim):
    summary = meta["facilities"]
    source = meta["source"]
    lines = [
        "# 부여군·청양군 통합 2km 도로망 검증",
        "",
        "## A. 완료 및 미완료",
        "",
        f"통합망 원본·단순화본과 시설 {summary['matched_facilities']}곳의 거리 비교를 생성했습니다. 대동마을회관 원본 행을 유지했습니다.",
        "실제 구호차량 통행 가능성은 현장 확인이 필요합니다.",
        "",
        "## B. 통합망 통계",
        "",
        f"PBF 기준 시각은 `{source['data_timestamp']}`이고 SHA-256은 `{source['sha256']}`입니다.",
        f"출처는 {source['upstream']}입니다. 데이터 검증은 `{source['verification']}` 기준입니다.",
        "",
        "| 그래프 | 노드 | 방향 간선 | 도로 km | 약연결요소 | 강연결요소 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, stats in {**meta["old_county_graphs"], **meta["graph"]["statistics"]}.items():
        lines.append(f"| {label} | {stats['nodes']:,} | {stats['directed_edges']:,} | "
                     f"{stats['undirected_length_m'] / 1000:,.3f} | {stats['weak_components']} | "
                     f"{stats['strong_components']} |")
    lines.extend([
        "",
        "통합망 도로 길이는 EPSG:5179 원본 way 구간을 방향 중복 없이 합산했습니다. 기존 표는 OSMnx 무방향 길이입니다.",
        "바깥 경계와 닿는 way의 전체 노드와 선형을 보존했습니다. 외부 way와 공유하는 경계 말단은 metadata.json에 기록했습니다.",
        f"원본 highway way {meta['source_extract']['intersecting_highway_ways']:,}개의 구간 수와 최종 방향별 구간 수를 "
        "way_audit.csv에 대조했습니다. 필터 제외 사유와 보존된 경계 횡단 way가 표에 포함됩니다.",
        "저장된 군별 그래프와 PBF의 기준 시각, 분석 범위와 길이 산법에는 차이가 있습니다.",
        "",
        "## C. 죽림리회관",
        "",
        f"청양군 기존 {jukrim['old_county_distance_m']:.6f}m, 부여군 비교 {jukrim['old_buyeo_comparison_m']:.6f}m, "
        f"보존 PBF {jukrim['preserved_pbf_reference_m']:.6f}m, 통합망 {jukrim['new_nearest']['distance_m']:.6f}m입니다.",
        f"보존 PBF와 동일한 전국 스냅샷 여부는 `{jukrim['compared_to_same_snapshot']}`입니다.",
        f"진입도로·교량·지천로의 공통 OSM 노드와 양방향 도달성을 기준으로 기존 누락 해소 여부는 "
        f"`{jukrim['reference_omission_resolved_in_tested_scope']}`입니다. 방향별 검사와 way ID는 jukrim.json에 있습니다.",
        "스냅샷 SHA-256이 다르면 도로 태그·노드·형상의 시점 차이도 거리와 연결 판정에 영향을 줄 수 있습니다.",
        "",
        "## D. 기존 6곳",
        "",
        "| 시설 | 기존 m | 통합 m | 변화 m | 판정 | 잔존 확인 |",
        "|---|---:|---:|---:|---|---|",
    ])
    for row in outliers:
        verdict = {"resolved": "해소", "remaining": "300m 초과 유지"}[row["outlier_300m_status"]]
        lines.append(f"| {row['시설명']} | {float(row['old_road_distance_m']):.3f} | "
                     f"{float(row['road_distance_m']):.3f} | {float(row['distance_change_m']):+.3f} | "
                     f"{verdict} | {row['remaining_review']} |")
    lines.extend([
        "", "가까운 drive 제외 도로와 제외 태그, 남천리의 최근접점 경계 위치, 천장리 말단의 공유 way는 outliers_existing_6.csv에 기록했습니다.",
        "", "## E. 146개 시설 비교", "",
        "| 항목 | 기존 | 통합 |", "|---|---:|---:|",
    ])
    for limit, values in summary["thresholds_m"].items():
        lines.append(f"| {limit}m 초과 | {values['before']} | {values['after']} |")
    for metric, display in (("mean", "평균"), ("median", "중앙값"), ("max", "최댓값"),
                            ("p90", "90백분위수"), ("p95", "95백분위수")):
        lines.append(f"| {display} m | {summary['old_distance'][metric + '_m']:.3f} | "
                     f"{summary['new_distance'][metric + '_m']:.3f} |")
    lines.extend([
        "",
        f"300m 초과 기존 시설 중 {summary['resolved_300m']}곳 해소, {summary['remaining_300m']}곳 유지, "
        f"{summary['new_300m']}곳 새로 탐지됐습니다. 매칭 실패는 {summary['failed_facilities']}건입니다.",
        f"거리 증가 {summary['distance_increased']}곳, 감소 {summary['distance_decreased']}곳, "
        f"동일 {summary['distance_equal']}곳입니다.",
        "",
        "## F. 미해결 문제",
        "",
        "좌표 독립 확인이 필요한 시설과 OSM drive 제외·미매핑 가능성은 상세 비교표와 독립 도로자료를 대조합니다. "
        "50·100·300m 기준은 기하학적 거리 검토에 사용합니다.",
        "",
        "## G. 파일 및 재현",
        "",
        "raw.graphml, simplified.graphml, network.gpkg, metadata.json, facilities_comparison_147.csv, "
        "outliers_existing_6.csv, outliers_newly_detected.csv, way_audit.csv, jukrim.json 및 이 보고서를 생성했습니다.",
        "원본 방향 구간·형상·태그, 단순화 전후 연결요소, GraphML·GeoPackage 재로딩, 시설 입력 불변과 거리 검증을 통과했습니다.",
        "재실행할 때는 --output에 새 경로를 지정합니다. --download-latest의 데이터 버전은 다운로드 시점에 따라 달라집니다.",
        "",
        "```sh",
        "python -m pip install -r requirements.txt -r requirements-pbf-audit.txt",
        meta["reproduce_command"],
        "```",
    ])
    (destination / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def input_files(root):
    evidence_dir = (root / REFERENCE).parent.parent
    return [
        root / AREA, root / access.CSV_PATH, root / OLD_CSV, root / REFERENCE,
        (root / AREA).parent / "validation.json", evidence_dir / "cause/conclusion.json",
        *(path for county in access.REGIONS for path in county_paths(root, county)),
    ]


def input_hashes(root):
    return {str(path.relative_to(root)): file_hash(path) for path in input_files(root)}


def build(root, source_path, destination, *, checksum=None, source=None, download_mode=None,
          allow_latest=False, command=None):
    root, destination = root.resolve(), destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Output directory already exists: {destination}")
    if destination.is_relative_to(root) and not destination.is_relative_to(root / "output/integrated_road_network"):
        raise ValueError("Output inside the repository must use output/integrated_road_network")
    if command is None:
        args = ["python", "-m", "scripts.build_integrated_road_network", "--output", str(destination)]
        if download_mode:
            args.append(f"--download-{download_mode}")
            if allow_latest:
                args.append("--allow-latest-if-unavailable")
        else:
            args.extend(["--pbf", str(source_path)])
            if checksum:
                args.extend(["--sha256", checksum])
            if source:
                args.extend(["--source", source])
        command = shlex.join(args)
    hashes = input_hashes(root)
    area, counties = load_area(root), load_counties(root)
    boundary_record = json.loads((root / AREA).with_name("validation.json").read_bytes())
    if boundary_record["outputs"][AREA.name]["sha256"] != hashes[str(AREA)]:
        raise ValueError("2 km boundary hash differs from the validated study area")
    if not all(area.covers(county) for county in counties.values()):
        raise ValueError("2 km region does not cover both source counties")
    original, columns, prior = load_facilities(root)
    evidence = json.loads((root / REFERENCE).read_bytes())
    with tempfile.TemporaryDirectory(prefix="integrated-roads-") as work:
        scratch = Path(work)
        download_metadata = None
        if download_mode:
            source_path, download_metadata = acquire_pbf(
                scratch, evidence["source"], historical=download_mode == "historical", allow_latest=allow_latest,
            )
            checksum, source = download_metadata["sha256"], download_metadata["source_url"]
        source_info = snapshot_metadata(source_path, evidence["source"], checksum, source, download_metadata)
        roads, extracted = read_pbf(source_path, area, scratch)
        rejected = drive_rejections(roads)
        raw, simplified, graph_checks = build_graphs(roads, rejected, area, counties, source_path)
        way_audit = audit_source_ways(roads, rejected, raw, simplified, graph_checks)
        results, index = compare_facilities(original, prior, raw, simplified, area)
        summary = summarize(results, prior)
        if summary["matched_facilities"] != 146 or summary["excluded_missing_coordinates"] != 1:
            failures = {str(access.identity(row)): row["road_access_error_message"] for row in results
                        if row["road_access_status"].startswith("failed_")}
            raise ValueError(f"Distance coverage failed: {failures}")
        if any(any(result[field] != row[field] for field in columns) for result, row in zip(results, original)):
            raise ValueError("Output changed a source facility field")
        old_graph = ox.load_graphml(county_paths(root, "청양군")[1])
        outliers = detailed_outliers(results, counties, old_graph, roads, rejected, raw, index)
        new_outliers = [row for row in results if row["outlier_300m_status"] == "new"]
        jukrim_row = next(row for row in results if access.identity(row) == ("청양군", "177"))
        jukrim = review_jukrim(raw, simplified, jukrim_row, evidence, source_info["same_as_archived_pbf"],
                               "integrated_2km_network")
        old_counties = {county: json.loads(county_paths(root, county)[2].read_bytes())
                        for county in access.REGIONS}
        meta = dict(
            status="completed", created_at_utc=datetime.now(timezone.utc).isoformat(),
            source=source_info, input_sha256=hashes,
            code_sha256={name: file_hash(root / name) for name in (
                "scripts/build_integrated_road_network.py", "scripts/integrated_road_graph.py",
                "scripts/integrated_pbf_source.py", "scripts/analyze_road_access.py", "scripts/jukrim_pbf_audit.py")},
            versions={"python": platform.python_version(), **{
                package: version(package) for package in
                ("osmnx", "osmium", "networkx", "shapely", "pyproj", "geopandas", "pyogrio", "numpy", "requests")
            }},
            parameters=dict(
                buffer_m=2000, area_m2=area.area, area_crs="EPSG:5179", geometry_crs="EPSG:4326",
                drive_filter=DRIVE, retain_all=True,
                extent_policy="All source highway ways whose EPSG:5179 line intersects or touches the 2 km polygon, with full way vertices; no county or outer clipping.",
                outside_network_policy="Outside-only ways omitted; shared external OSM nodes and omitted drive way IDs audited.",
                distance_tie_tolerance_m=access.TIE_TOLERANCE_M,
                physical_length_policy="EPSG:5179 sum once per original (OSM way ID, segment ordinal).",
                consecutive_identical_reference_policy="No movement edge for consecutive appearances of the same OSM node ID.",
                coincident_node_distance_policy="Zero-length source segments are retained in topology; distance uses positive-length road lines.",
                relaxation="Keep each way's endpoints, differing way IDs, tagged nodes, parallel edge endpoints, and audited external contacts.",
            ),
            source_extract={**extracted, "drive_admitted_ways": sum(not value for value in rejected.values()),
                            "drive_excluded_ways": sum(bool(value) for value in rejected.values())},
            old_county_graphs={county: info["statistics"] for county, info in old_counties.items()},
            old_graph_extracted_at_utc={county: info["extracted_at_utc"] for county, info in old_counties.items()},
            graph=graph_checks, facilities=summary, reproduce_command=command,
        )
        if file_hash(source_path) != source_info["sha256"]:
            raise ValueError("Source PBF changed during analysis")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".integrated-road-staging-", dir=destination.parent) as staging:
            payload = Path(staging) / "results"
            payload.mkdir()
            export_graphs(payload, raw, simplified)
            save_csv(payload / "facilities_comparison_147.csv", columns + OUTPUT_COLUMNS, results)
            save_csv(payload / "outliers_existing_6.csv", DETAIL_COLUMNS, outliers)
            save_csv(payload / "outliers_newly_detected.csv", columns + OUTPUT_COLUMNS, new_outliers)
            save_csv(payload / "way_audit.csv", WAY_AUDIT_FIELDS, way_audit)
            write_json(payload / "jukrim.json", jukrim)
            render_report(payload, meta, outliers, jukrim)
            if input_hashes(root) != hashes or file_hash(source_path) != source_info["sha256"]:
                raise ValueError("Input files changed during execution")
            meta["verification"] = dict(
                inputs_unchanged=True, graphml_and_gpkg_roundtrips=True, facility_fields_unchanged=True,
                raw_simplified_nearest_agreement=146, exhaustive_outlier_distance_checks=6,
                compared_source_highway_ways=len(way_audit), missing_drive_way_segments=0,
            )
            meta["output_sha256"] = {path.name: file_hash(path) for path in payload.iterdir()}
            write_json(payload / "metadata.json", meta)
            if destination.exists():
                raise FileExistsError(f"Output directory already exists: {destination}")
            payload.rename(destination)
    return meta


def replay_jukrim(root):
    """Rebuild only the real, preserved single-site source subset through the new graph code."""
    hashes = input_hashes(root)
    evidence = json.loads((root / REFERENCE).read_bytes())
    _, _, prior = load_facilities(root)
    row = prior["청양군", "177"]
    point = Point(access.TO_METRES.transform(float(row["longitude"]), float(row["latitude"])))
    area, counties = load_area(root), load_counties(root)
    roads = {
        r["way_id"]: dict(coords=r["geometry"]["coordinates"], nodes=r["nodes"], tags=r["tags"])
        for r in evidence["roads"]
        if area.intersects(metric_line(r["geometry"]["coordinates"]))
    }
    raw, simplified, graph_checks = build_graphs(roads, drive_rejections(roads), area, counties)
    before = spatial_index(raw).nearest(point)
    after = spatial_index(simplified).nearest(point)
    if abs(before["distance"] - after["distance"]) > 1e-7:
        raise ValueError("Jukrim distance changed after simplification")
    nearest = dict(road_distance_m=after["distance"], old_road_distance_m=row["road_distance_m"],
                   road_edge_osmid=simplified.edges[after["key"]]["osmid"])
    result = review_jukrim(raw, simplified, nearest, evidence, True, "preserved_jukrim_subset")
    if input_hashes(root) != hashes:
        raise ValueError("Input files changed during local replay")
    result.update(
        input_sha256=hashes, input_source_ways=len(evidence["roads"]), selected_subset_ways=len(roads),
        national_pbf_opened=False, network_requests=0,
        validation=graph_checks, source_data_timestamp=evidence["source"]["data_timestamp"],
        source_pbf_sha256=evidence["source"]["sha256"],
    )
    return result


def main():
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pbf", type=Path)
    mode.add_argument("--download-historical", action="store_const", const="historical", dest="download_mode",
                      help="Download and verify the archived 2026-11-09 South Korea PBF")
    mode.add_argument("--download-latest", action="store_const", const="latest", dest="download_mode")
    mode.add_argument("--replay-jukrim", action="store_true")
    parser.add_argument("--sha256", help="Verified checksum for a different local snapshot")
    parser.add_argument("--source", help="Upstream identifier or URL for a different local snapshot")
    parser.add_argument("--allow-latest-if-unavailable", action="store_true",
                        help="Allow latest official PBF if the historical download is unavailable")
    parser.add_argument("--output", type=Path, required=True, help="New result directory or replay JSON")
    args = parser.parse_args()
    if args.pbf is None and (args.sha256 or args.source):
        parser.error("--sha256 and --source apply only to --pbf")
    if args.allow_latest_if_unavailable and args.download_mode != "historical":
        parser.error("--allow-latest-if-unavailable applies only to --download-historical")
    try:
        if args.replay_jukrim:
            result = replay_jukrim(ROOT)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            print(f"Jukrim local replay: {result['new_nearest']['distance_m']:.9f} m -> {args.output}")
        else:
            command = shlex.join(["python", "-m", "scripts.build_integrated_road_network", *sys.argv[1:]])
            meta = build(ROOT, args.pbf, args.output, checksum=args.sha256, source=args.source,
                         download_mode=args.download_mode, allow_latest=args.allow_latest_if_unavailable,
                         command=command)
            print(json_text(meta["facilities"]))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Integrated road analysis failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
