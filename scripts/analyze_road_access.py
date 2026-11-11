"""Stage 2: geometric association with existing driving edges, without editing graphs.

Run: python scripts/analyze_road_access.py
Tests: python -m pytest -q tests/test_road_access.py
All distance calculations use EPSG:5179. No network/API requests are made.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import platform

import folium
import networkx as nx
import numpy as np
import osmnx as ox
from pyproj import Transformer
import pyproj
import shapely
from shapely.geometry import Point, mapping
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = Path('data/processed/shelters_final_147.csv')
NETWORK_ROOT = Path('data/20261109T045434949410Z')
REGIONS = {'부여군': 'buyeo', '청양군': 'cheongyang'}
METRIC_CRS = 'EPSG:5179'
TO_METRES = Transformer.from_crs(4326, 5179, always_xy=True)
TO_WGS84 = Transformer.from_crs(5179, 4326, always_xy=True)
TIE_TOLERANCE_M = 1e-7
NOTE = ('최근접 자동차 도로 간선과의 기하학적 대응입니다. 실제 시설 진입로·보행/차량 통행 가능성은 '
        '미확인입니다. WCC는 방향을 무시한 연결성으로, 방향별 도달 가능성을 보장하지 않습니다.')
NEW_COLUMNS = [
    'road_access_status', 'road_access_exclusion_reason', 'road_access_error_code',
    'road_access_error_message', 'road_distance_crs', 'road_facility_x_5179',
    'road_facility_y_5179', 'road_edge_u', 'road_edge_v', 'road_edge_key',
    'road_edge_osmid', 'road_edge_highway', 'road_edge_name', 'road_geometry_source',
    'road_edge_geometry_length_m', 'road_distance_m', 'road_nearest_x_5179',
    'road_nearest_y_5179', 'road_nearest_longitude', 'road_nearest_latitude',
    'road_wcc_id', 'road_wcc_node_count', 'road_wcc_directed_edge_count',
    'road_wcc_is_largest', 'road_tied_edge_count', 'road_tied_wcc_ids',
    'road_review_gt_50m', 'road_review_gt_100m', 'road_review_reason', 'road_access_note',
]


def identity(row):
    return row['시군명'].strip(), row['시설일련번호'].strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict, tuple)) else str(value)


def wcc_index(graph, slug):
    """Stable IDs: descending node count, then smallest original integer node ID."""
    components = sorted(nx.weakly_connected_components(graph), key=lambda c: (-len(c), min(c)))
    if not components:
        raise ValueError('EMPTY_GRAPH')
    maximum = len(components[0])
    nodes_to_component, summaries = {}, {}
    for rank, nodes in enumerate(components, 1):
        cid = f'{slug}_wcc_{rank:03d}'
        summaries[cid] = {'component_id': cid, 'node_count': len(nodes),
            'node_share': len(nodes) / graph.number_of_nodes(),
            'directed_edge_count': graph.subgraph(nodes).number_of_edges(),
            'is_largest': len(nodes) == maximum,
            'smallest_osm_node_id': min(nodes)}
        nodes_to_component.update({node: cid for node in nodes})
    return nodes_to_component, summaries


class RoadIndex:
    def __init__(self, graph, slug):
        if not isinstance(graph, nx.MultiDiGraph) or not graph.is_directed():
            raise ValueError('EXPECTED_DIRECTED_MULTIGRAPH')
        self.slug = slug
        self.node_wcc, self.components = wcc_index(graph, slug)
        self.original_nodes = graph.number_of_nodes()
        self.original_edges = graph.number_of_edges()
        sources = {}
        for u, v, k, data in graph.edges(keys=True, data=True):
            sources[u, v, k] = 'graphml_linestring' if data.get('geometry') is not None else 'original_node_endpoints'
            if data.get('geometry') is not None and data['geometry'].geom_type != 'LineString':
                raise ValueError('NON_LINESTRING_EDGE')
        # OSMnx projects stored curved geometry and nodes. For an edge lacking a
        # geometry attribute, graph_to_gdfs uses that original edge's two nodes.
        self.projected = ox.project_graph(graph, to_crs=METRIC_CRS)
        if (set(self.projected.nodes) != set(graph.nodes) or
                set(self.projected.edges(keys=True)) != set(graph.edges(keys=True))):
            raise ValueError('GRAPH_TOPOLOGY_CHANGED_DURING_PROJECTION')
        edges = ox.graph_to_gdfs(self.projected, nodes=False, fill_edge_geometry=True)
        self.edges = edges.sort_index()
        self.keys = list(self.edges.index)
        self.lines = list(self.edges.geometry)
        if not self.lines:
            raise ValueError('EMPTY_EDGE_SET')
        if any(line.geom_type != 'LineString' or line.is_empty or not line.is_valid or
               not math.isfinite(line.length) or line.length <= 0 for line in self.lines):
            # Failing this region avoids silently omitting a potentially nearest edge.
            raise ValueError('INVALID_EDGE_GEOMETRY')
        self.geometry_sources = sources
        self.tree = STRtree(self.lines)

    def nearest(self, point):
        if point.is_empty or not math.isfinite(point.x) or not math.isfinite(point.y):
            raise ValueError('INVALID_PROJECTED_FACILITY')
        first = int(self.tree.nearest(point))
        minimum = point.distance(self.lines[first])
        # Opposite directions often share geometry. Include numerical ties and
        # choose a deterministic original (u,v,key); do not infer travel direction.
        candidates = self.tree.query(point, predicate='dwithin', distance=minimum + TIE_TOLERANCE_M)
        distances = {int(i): point.distance(self.lines[int(i)]) for i in candidates}
        best = min(distances.values())
        tied = sorted((i for i, d in distances.items() if d <= best + TIE_TOLERANCE_M),
                      key=lambda i: self.keys[i])
        i = tied[0]
        line = self.lines[i]
        snap = line.interpolate(line.project(point))
        distance = point.distance(snap)
        if abs(distance - point.distance(line)) > 1e-6 or line.distance(snap) > 1e-6:
            raise ValueError('NEAREST_POINT_VERIFICATION_FAILED')
        u, v, k = self.keys[i]
        cid = self.node_wcc[u]
        if cid != self.node_wcc[v]:
            raise ValueError('EDGE_COMPONENT_MISMATCH')
        return {'key': (int(u), int(v), int(k)), 'index': i, 'point': snap,
                'distance': distance, 'line': line, 'component': self.components[cid],
                'tie_count': len(tied),
                'tied_component_ids': sorted({self.node_wcc[self.keys[j][0]] for j in tied})}


def analyze_row(row, networks, network_errors=None):
    result = {col: '' for col in NEW_COLUMNS}
    result.update(road_distance_crs=METRIC_CRS, road_access_note=NOTE)
    result = {**row, **result}
    lat, lon = row['latitude'].strip(), row['longitude'].strip()
    if not lat and not lon:
        result.update(road_access_status='excluded_missing_coordinates',
                      road_access_exclusion_reason='좌표 미확인으로 도로 최근접점·연결성 분석에서 제외',
                      road_access_error_code='MISSING_COORDINATES')
        return result
    try:
        y, x = float(lat), float(lon)
        if not math.isfinite(y) or not math.isfinite(x) or not (-90 <= y <= 90 and -180 <= x <= 180):
            raise ValueError
    except (ValueError, TypeError):
        result.update(road_access_status='failed_invalid_coordinates', road_access_error_code='INVALID_COORDINATES',
                      road_access_error_message='위도·경도 누락, 비숫자, 비유한 값 또는 범위 오류')
        return result
    county = row['시군명'].strip()
    if county not in networks:
        result.update(road_access_status='failed_network', road_access_error_code='NETWORK_UNAVAILABLE',
                      road_access_error_message=(network_errors or {}).get(county, '해당 시군 도로망 없음'))
        return result
    network = networks[county]
    try:
        px, py = TO_METRES.transform(x, y, errcheck=True)
        match = network.nearest(Point(px, py))
        sx, sy = match['point'].x, match['point'].y
        slon, slat = TO_WGS84.transform(sx, sy, errcheck=True)
        edge = network.edges.iloc[match['index']]
        component = match['component']
        u, v, k = match['key']
        distance = match['distance']
        result.update(road_access_status='matched',
            road_facility_x_5179=f'{px:.6f}', road_facility_y_5179=f'{py:.6f}',
            road_edge_u=str(u), road_edge_v=str(v), road_edge_key=str(k),
            road_edge_osmid=json_text(edge.get('osmid', '')),
            road_edge_highway=json_text(edge.get('highway', '')),
            road_edge_name='' if isinstance(edge.get('name', ''), float) and math.isnan(edge.get('name', '')) else json_text(edge.get('name', '')),
            road_geometry_source=network.geometry_sources[u, v, k],
            road_edge_geometry_length_m=f"{match['line'].length:.6f}",
            road_distance_m=f'{distance:.6f}', road_nearest_x_5179=f'{sx:.6f}', road_nearest_y_5179=f'{sy:.6f}',
            road_nearest_longitude=f'{slon:.10f}', road_nearest_latitude=f'{slat:.10f}',
            road_wcc_id=component['component_id'], road_wcc_node_count=str(component['node_count']),
            road_wcc_directed_edge_count=str(component['directed_edge_count']),
            road_wcc_is_largest=str(component['is_largest']).lower(),
            road_tied_edge_count=str(match['tie_count']),
            road_tied_wcc_ids=json.dumps(match['tied_component_ids']),
            road_review_gt_50m=str(distance > 50).lower(), road_review_gt_100m=str(distance > 100).lower(),
            road_review_reason='distance_gt_100m' if distance > 100 else 'distance_gt_50m' if distance > 50 else '')
    except Exception as exc:
        # Keep genuine failures with no fabricated edge, point, distance, or WCC.
        result.update(road_access_status='failed_nearest_edge', road_access_error_code=type(exc).__name__,
                      road_access_error_message=str(exc)[:300])
    return result


def distance_stats(rows):
    distances = np.array([float(r['road_distance_m']) for r in rows if r['road_access_status'] == 'matched'])
    if not len(distances):
        return {'count': 0, 'min_m': None, 'median_m': None, 'mean_m': None,
                'p90_m': None, 'p95_m': None, 'max_m': None}
    return {'count': len(distances), 'min_m': float(distances.min()),
            'median_m': float(np.median(distances)), 'mean_m': float(distances.mean()),
            'p90_m': float(np.percentile(distances, 90)), 'p95_m': float(np.percentile(distances, 95)),
            'max_m': float(distances.max())}


def facility_detail(row):
    fields = ['시군명', '시설일련번호', '시설명', 'geocoding_source', 'road_access_status',
              'road_distance_m', 'road_edge_u', 'road_edge_v', 'road_edge_key',
              'road_wcc_id', 'road_wcc_is_largest', 'road_review_gt_50m',
              'road_review_gt_100m', 'road_access_error_code', 'road_access_error_message',
              'road_access_exclusion_reason', 'road_geometry_source']
    return {k: row[k] for k in fields}


def build_report(original, results, columns, networks, network_errors, inputs):
    matched = [r for r in results if r['road_access_status'] == 'matched']
    excluded = [r for r in results if r['road_access_status'].startswith('excluded_')]
    failed = [r for r in results if r['road_access_status'].startswith('failed_')]
    eligible = len(results) - len(excluded)
    assert len(original) == len(results) == 147
    assert all(all(a[c] == b[c] for c in columns) for a, b in zip(original, results))
    assert len({identity(r) for r in results}) == 147
    assert Counter(r['시군명'].strip() for r in results) == {'부여군': 30, '청양군': 117}
    assert eligible == 146 and len(excluded) == 1 and excluded[0]['시설명'] == '대동마을회관'
    assert excluded[0]['geocoding_status'] == 'location_unverified'
    assert excluded[0]['latitude'] == excluded[0]['longitude'] == ''
    assert all(not excluded[0][c] for c in ('road_edge_u', 'road_edge_v', 'road_edge_key', 'road_distance_m', 'road_wcc_id'))
    regions = {}
    for county in REGIONS:
        subset = [r for r in results if r['시군명'].strip() == county]
        done = [r for r in subset if r['road_access_status'] == 'matched']
        attempted = [r for r in subset if not r['road_access_status'].startswith('excluded_')]
        assert len(attempted) == (30 if county == '부여군' else 116)
        network = networks.get(county)
        counts = Counter(r['road_wcc_id'] for r in done)
        components = ([{**d, 'matched_facilities': counts.get(cid, 0)} for cid, d in network.components.items()]
                      if network else [])
        regions[county] = {'total_facilities': len(subset), 'analyzed_facilities': len(attempted),
            'matched_facilities': len(done), 'match_rate_among_coordinate_holders': len(done)/len(attempted),
            'distance_statistics': distance_stats(subset),
            'review_gt_50m': sum(r['road_review_gt_50m'] == 'true' for r in done),
            'review_gt_100m': sum(r['road_review_gt_100m'] == 'true' for r in done),
            'facilities_on_nonlargest_wcc': sum(r['road_wcc_is_largest'] == 'false' for r in done),
            'original_nodes': network.original_nodes if network else None,
            'original_directed_edges': network.original_edges if network else None,
            'edge_geometry_sources': dict(Counter(network.geometry_sources.values())) if network else {},
            'matched_edge_geometry_sources': dict(Counter(r['road_geometry_source'] for r in done)),
            'wcc_count': len(components), 'components': components,
            'network_error': network_errors.get(county)}
    serials = Counter(r['시설일련번호'] for r in results)
    descending = sorted(matched, key=lambda r: float(r['road_distance_m']), reverse=True)
    return {
        'stage': '2 - geometric nearest driving edge and weak connectivity',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'inputs_sha256': inputs, 'distance_crs': METRIC_CRS, 'map_coordinate_crs': 'EPSG:4326',
        'versions': {'python': platform.python_version(), 'osmnx': ox.__version__,
                     'networkx': nx.__version__, 'shapely': shapely.__version__, 'pyproj': pyproj.__version__},
        'counts': {'total_facilities': len(results), 'coordinate_holders': eligible,
                   'matched_facilities': len(matched), 'failed_facilities': len(failed),
                   'excluded_facilities': len(excluded),
                   'match_rate_among_coordinate_holders': len(matched)/eligible,
                   'matched_share_of_all_147_facilities': len(matched)/147,
                   'kakao_coordinate_holders': sum(r['geocoding_status']=='success' for r in original),
                   'manual_coordinate_holders': sum(r['geocoding_status']=='success_manual' for r in original)},
        'regions': regions, 'overall_distance_statistics': distance_stats(results),
        'review_thresholds': {'unit': 'm', 'strictly_greater_than': [50, 100],
                             'gt_100m_is_subset_of_gt_50m': True,
                             'meaning': '연구용 점검 기준. 통행 가능성 판정 기준이 아닙니다.'},
        'outliers_gt_50m': [facility_detail(r) for r in descending if r['road_review_gt_50m']=='true'],
        'outliers_gt_100m': [facility_detail(r) for r in descending if r['road_review_gt_100m']=='true'],
        'nonlargest_wcc_facilities': [facility_detail(r) for r in matched if r['road_wcc_is_largest']=='false'],
        'failures': [facility_detail(r) for r in failed], 'excluded': [facility_detail(r) for r in excluded],
        'methodology': {'nearest_point': 'LineString.interpolate(LineString.project(facility)) in EPSG:5179',
            'candidate_search': 'Shapely STRtree over all original directed edges in the facility county',
            'tie_policy': 'Choose smallest numeric (u,v,key) among distances within tolerance of the minimum',
            'tie_tolerance_m': TIE_TOLERANCE_M,
            'matched_facilities_with_tied_edges': sum(int(r['road_tied_edge_count'])>1 for r in matched),
            'matched_facilities_with_ties_across_components': sum(len(json.loads(r['road_tied_wcc_ids']))>1 for r in matched),
            'wcc_id_policy': 'County slug + descending node-count rank; ties sorted by minimum original OSM node ID',
            'largest_component_definition': 'Maximum node count, all equal-size maxima qualify',
            'geometry_without_stored_linestring': 'OSMnx graph_to_gdfs fills the existing edge geometry from its two original nodes; separately flagged. No new road or graph edge.',
            'edge_direction': 'Recorded directed key does not infer arrival/departure direction',
            'distance': 'Planar straight-line distance from facility to existing edge geometry, not route distance'},
        'validation': {'passed': True, 'all_original_columns_and_values_preserved': True,
                       'facility_key': ['시군명', '시설일련번호'], 'composite_keys_unique': True,
                       'global_serial_duplicate_groups': sum(n>1 for n in serials.values()),
                       'original_files_unchanged': True},
        'data_quality': {
            'review_gt_50m_count': sum(r['road_review_gt_50m']=='true' for r in matched),
            'review_gt_100m_count': sum(r['road_review_gt_100m']=='true' for r in matched),
            'largest_distance_facility': facility_detail(descending[0]) if descending else None,
            'large_distance_interpretation': '좌표 정확도, OSM 도로 누락, 기존 도로망 범위·자동차 도로 추출 기준 등을 검토해야 합니다. 원인을 확정하거나 좌표를 보정하지 않았습니다.',
            'node_defined_geometry_matches': [facility_detail(r) for r in matched if r['road_geometry_source']=='original_node_endpoints'],
            'manual_coordinates_not_field_verified': 4,
            'county_scoped_serials': '시설일련번호만으로는 군 간 중복이 있어 시군명+시설일련번호로 식별했습니다.'},
        'limitations': [NOTE, '50m·100m는 연구용 점검 기준이며 진입로의 실재·접근 가능성을 보장하지 않습니다.',
            '수동 좌표 4개는 Google Maps (manual)이며 현장 검증을 거치지 않았습니다.',
            '대동마을회관은 좌표 미확인으로 원본 행을 유지하고 분석에서 제외했습니다.',
            '각 시설의 소속 군 도로망 안에서 최근접점을 찾았습니다. 인접 군 도로와 비교하지 않았습니다.',
            '기존 OSM 자동차 도로망의 추출 시점·누락·정확도 및 형상 표현 한계를 승계합니다.',
            'WCC는 물리적인 간선 교차를 자동 연결하지 않으며 방향·회전제한·시설 진입 허가를 검사하지 않습니다.',
            'HTML의 배경 타일과 Leaflet 라이브러리는 인터넷이 필요합니다.'],
        'scope': {'road_redownloaded': False, 'geocoding_called': False, 'nodes_or_edges_added': False,
                  'graphml_modified': False, 'flood_or_warehouse_processing': False,
                  'shortest_routes_calculated': False, 'optimization_performed': False},
    }


def render_map(county, network, rows, destination):
    done = [r for r in rows if r['시군명'].strip() == county and r['road_access_status']=='matched']
    points = [[float(r['latitude']), float(r['longitude'])] for r in done]
    map_ = folium.Map(location=points[0] if points else [36.3, 126.9], zoom_start=11,
                      tiles='OpenStreetMap', prefer_canvas=True)
    if network:
        wgs_edges = network.edges.to_crs(4326)
        folium.GeoJson(json.loads(wgs_edges[['geometry']].to_json()), name='기존 OSMnx 자동차 도로망',
                       style_function=lambda _: {'color': '#667788', 'weight': 2, 'opacity': .5}).add_to(map_)
    shelters = folium.FeatureGroup(name=f'시설 위치 ({len(points)}곳)').add_to(map_)
    snaps = folium.FeatureGroup(name=f'도로 위 최근접점 ({len(points)}곳)').add_to(map_)
    connectors = folium.FeatureGroup(name='시설→최근접점 점선 (도로 아님)').add_to(map_)
    for r in done:
        a = [float(r['latitude']), float(r['longitude'])]
        b = [float(r['road_nearest_latitude']), float(r['road_nearest_longitude'])]
        info = [r['시설명'], f"{r['시군명']} · 시설일련번호 {r['시설일련번호']}", r['상세주소'],
                f"좌표 출처: {r['geocoding_source']}",
                f"도로까지 직선거리: {float(r['road_distance_m']):.2f} m",
                f"간선: ({r['road_edge_u']}, {r['road_edge_v']}, {r['road_edge_key']})",
                f"WCC: {r['road_wcc_id']} / 최대 요소: {r['road_wcc_is_largest']}",
                f"연구 검토 기준: 50m 초과={r['road_review_gt_50m']}, 100m 초과={r['road_review_gt_100m']}", NOTE]
        if r['manual_validation_note']:
            info.append(r['manual_validation_note'])
        popup = '<br>'.join(html.escape(v) for v in info)
        folium.CircleMarker(a, radius=6, color='#ef6c00' if r['geocoding_status']=='success_manual' else '#1565c0',
            fill=True, fill_opacity=.9, tooltip=html.escape(r['시설명']), popup=folium.Popup(popup, max_width=480)).add_to(shelters)
        folium.CircleMarker(b, radius=4, color='#00897b', fill=True, fill_opacity=1,
            tooltip=html.escape(r['시설명'] + ' 도로 위 최근접점'), popup=folium.Popup(popup, max_width=480)).add_to(snaps)
        # This visual guide is only a map overlay. It is never inserted in a graph.
        folium.PolyLine([a, b], color='#8e24aa', weight=2, dash_array='5, 6',
            tooltip=f"{html.escape(r['시설명'])}: {float(r['road_distance_m']):.2f} m").add_to(connectors)
    excluded_note = '대동마을회관: 좌표 미확인으로 지도·분석 제외.' if county=='청양군' else ''
    legend = f'''<div style="position:fixed;bottom:20px;left:15px;z-index:9999;background:white;padding:12px;border:1px solid #777;max-width:460px">
    <b>{county} · 도로 최근접점 분석 {len(points)}곳</b><br>
    파랑: API 시설 좌표 / 주황: 수동 시설 좌표 (현장 미검증)<br>
    초록: 도로 위 최근접점 / 보라 점선: 직선 거리 표시 (도로 아님)<br>
    실제 시설 진입로 또는 통행 가능성을 확정하지 않습니다.<br>
    50m·100m는 연구용 점검 기준입니다. {excluded_note}<br>
    거리 계산 EPSG:5179 · 표시 EPSG:4326 · OSM / ODbL</div>'''
    map_.get_root().html.add_child(folium.Element(legend))
    if points:
        map_.fit_bounds([[min(p[0] for p in points), min(p[1] for p in points)],
                         [max(p[0] for p in points), max(p[1] for p in points)]])
    folium.LayerControl(collapsed=False).add_to(map_)
    map_.save(str(destination))
    # Remove generated whitespace without changing map content.
    destination.write_text('\n'.join(line.rstrip() for line in destination.read_text().splitlines())+'\n', encoding='utf-8')


def run(root=ROOT, results_root=None):
    results_root = root if results_root is None else results_root
    source = root / CSV_PATH
    source_bytes = source.read_bytes()
    with source.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        original = list(reader)
        columns = reader.fieldnames
    if not columns or set(NEW_COLUMNS).intersection(columns):
        raise ValueError('원본 시설 CSV만 입력할 수 있습니다.')
    if len(original)!=147 or len({identity(r) for r in original})!=147:
        raise ValueError('원본 147행 및 복합 식별자 검증 실패')
    inputs = {str(CSV_PATH): hashlib.sha256(source_bytes).hexdigest()}
    networks, errors = {}, {}
    for county, slug in REGIONS.items():
        path = root / NETWORK_ROOT / slug / 'network.graphml'
        if path.exists():
            inputs[str(path.relative_to(root))] = digest(path)
        try:
            networks[county] = RoadIndex(ox.load_graphml(path), slug)
        except Exception as exc:
            errors[county] = f'{type(exc).__name__}: {str(exc)[:300]}'
    results = [analyze_row(row, networks, errors) for row in original]
    report = build_report(original, results, columns, networks, errors, inputs)
    paths = [results_root/'data/processed/shelters_road_access_147.csv',
             results_root/'data/processed/shelters_road_access_report.json',
             results_root/'output/road_access/buyeo_map.html', results_root/'output/road_access/cheongyang_map.html']
    if any(path.exists() for path in paths):
        raise FileExistsError('새 결과 경로가 이미 존재합니다. 기존 파일을 덮어쓰지 않습니다.')
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    with paths[0].open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns+NEW_COLUMNS)
        writer.writeheader(); writer.writerows(results)
    for county, slug in REGIONS.items():
        render_map(county, networks.get(county), results, results_root/f'output/road_access/{slug}_map.html')
    for path, checksum in inputs.items():
        if digest(root/path) != checksum:
            raise ValueError('원본 입력 파일 변경 감지: '+path)
    paths[1].write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({'counts': report['counts'], 'regions': {k: {field: v[field] for field in
        ('distance_statistics', 'review_gt_50m', 'review_gt_100m', 'facilities_on_nonlargest_wcc')}
        for k, v in report['regions'].items()}}, ensure_ascii=False))
    return 1 if report['counts']['failed_facilities'] else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--results-root', type=Path,
                        help='기존 결과를 보존하며 재현할 때 새 출력 루트 지정 (예: work/road_access_reproduction)')
    args = parser.parse_args()
    raise SystemExit(run(args.root, args.results_root))
