"""Geometry/unit fixtures below are tests, never substituted for research data."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import networkx as nx
import osmnx as ox
from pyproj import Transformer
import pytest
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('road_access', ROOT/'scripts/analyze_road_access.py')
road = importlib.util.module_from_spec(spec)
spec.loader.exec_module(road)


def graph_fixture():
    g = nx.MultiDiGraph(crs='EPSG:5179', simplified=True)
    g.add_node(1, x=0., y=0.)
    g.add_node(2, x=100., y=0.)
    g.add_edge(1, 2, key=0, geometry=LineString([(0, 0), (0, 100), (100, 100), (100, 0)]), length=300.)
    return g


def test_curved_edge_interior_is_closer_than_either_intersection():
    graph = graph_fixture()
    match = road.RoadIndex(graph, 'test').nearest(Point(50, 110))
    assert match['point'].distance(Point(50, 100)) < 1e-9
    assert match['distance'] == pytest.approx(10)
    assert min(Point(50, 110).distance(Point(0, 0)), Point(50, 110).distance(Point(100, 0))) > 100
    assert set(graph.nodes) == {1, 2} and graph.number_of_edges() == 1


def test_wcc_uses_weak_direction_and_deterministic_size_order():
    graph = graph_fixture()
    graph.add_node(3, x=100., y=20.)
    graph.add_edge(2, 3, length=20.)
    graph.add_node(10, x=1000., y=1000.)
    graph.add_node(11, x=1100., y=1000.)
    graph.add_edge(10, 11, length=100.)
    index = road.RoadIndex(graph, 'test')
    assert index.node_wcc[1] == index.node_wcc[3] == 'test_wcc_001'
    assert not nx.is_strongly_connected(graph.subgraph([1, 2, 3]))
    assert index.components['test_wcc_001']['is_largest'] is True
    small = index.nearest(Point(1050, 1010))
    assert small['component']['component_id'] == 'test_wcc_002'
    assert small['component']['is_largest'] is False


def test_parallel_direction_tie_is_stable_and_does_not_connect_components():
    graph = graph_fixture()
    line = graph.edges[1, 2, 0]['geometry']
    graph.add_edge(2, 1, key=0, geometry=LineString(list(line.coords)[::-1]), length=300.)
    graph.add_edge(1, 2, key=1, geometry=line, length=300.)
    index = road.RoadIndex(graph, 'test')
    match = index.nearest(Point(50, 110))
    assert match['key'] == (1, 2, 0)
    assert match['tie_count'] == 3
    assert match['tied_component_ids'] == ['test_wcc_001']


def test_missing_stored_geometry_uses_only_original_edge_endpoints():
    graph = graph_fixture()
    del graph.edges[1, 2, 0]['geometry']
    index = road.RoadIndex(graph, 'test')
    assert index.geometry_sources[1, 2, 0] == 'original_node_endpoints'
    assert index.nearest(Point(50, 10))['point'].distance(Point(50, 0)) < 1e-9
    assert 'geometry' not in graph.edges[1, 2, 0]


def test_missing_and_invalid_coordinates_have_no_fabricated_result():
    missing = {'시군명': '청양군', '시설일련번호': '242', '시설명': '대동마을회관',
               'latitude': '', 'longitude': '', 'geocoding_status': 'location_unverified'}
    result = road.analyze_row(missing, {})
    assert result['road_access_status'] == 'excluded_missing_coordinates'
    assert result['geocoding_status'] == 'location_unverified'
    for col in ('road_edge_u', 'road_edge_v', 'road_edge_key', 'road_distance_m', 'road_wcc_id',
                'road_nearest_x_5179', 'road_nearest_longitude'):
        assert result[col] == ''
    for lat, lon in [('NaN', '127'), ('36', ''), ('91', '127'), ('36', '181')]:
        r = road.analyze_row({**missing, 'latitude': lat, 'longitude': lon}, {})
        assert r['road_access_status'] == 'failed_invalid_coordinates'
        assert r['road_distance_m'] == r['road_edge_u'] == ''


def test_network_failure_records_cause_without_synthetic_match():
    result = road.analyze_row({'시군명': '청양군', 'latitude': '36.4', 'longitude': '126.9'},
                              {}, {'청양군': 'INVALID_EDGE_GEOMETRY'})
    assert result['road_access_status'] == 'failed_network'
    assert result['road_access_error_message'] == 'INVALID_EDGE_GEOMETRY'
    assert result['road_distance_m'] == result['road_nearest_x_5179'] == ''


@pytest.mark.parametrize('distance,flag50,flag100', [(50, 'false', 'false'), (50.01, 'true', 'false'),
                                                   (100, 'true', 'false'), (100.01, 'true', 'true')])
def test_strict_review_thresholds(distance, flag50, flag100):
    # A real projected test line at a specified offset from a WGS84 test point.
    x, y = road.TO_METRES.transform(126.9, 36.4)
    g = nx.MultiDiGraph(crs='EPSG:5179', simplified=True)
    g.add_node(1, x=x-100, y=y-distance)
    g.add_node(2, x=x+100, y=y-distance)
    g.add_edge(1, 2, geometry=LineString([(x-100, y-distance), (x+100, y-distance)]), length=200.)
    row = {'시군명': '청양군', 'latitude': '36.4', 'longitude': '126.9'}
    result = road.analyze_row(row, {'청양군': road.RoadIndex(g, 'test')})
    assert result['road_access_status'] == 'matched'
    assert result['road_review_gt_50m'] == flag50
    assert result['road_review_gt_100m'] == flag100


@pytest.fixture(scope='module')
def real_results():
    path = ROOT/'data/processed/shelters_road_access_147.csv'
    with path.open(encoding='utf-8-sig', newline='') as f:
        results = list(csv.DictReader(f))
    report = json.loads((ROOT/'data/processed/shelters_road_access_report.json').read_text())
    with (ROOT/road.CSV_PATH).open(encoding='utf-8-sig', newline='') as f:
        original = list(csv.DictReader(f))
    return original, results, report


def test_real_output_preserves_all_original_fields_and_input_hashes(real_results):
    original, results, report = real_results
    assert len(original) == len(results) == 147
    assert len({road.identity(r) for r in results}) == 147
    for source, output in zip(original, results):
        assert all(source[c] == output[c] for c in source)
    assert sum(r['road_access_status']=='matched' for r in results) == 146
    missing = [r for r in results if r['road_access_status']=='excluded_missing_coordinates']
    assert len(missing)==1 and missing[0]['시설명']=='대동마을회관'
    assert report['regions']['부여군']['analyzed_facilities'] == 30
    assert report['regions']['청양군']['analyzed_facilities'] == 116
    for path, checksum in report['inputs_sha256'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest() == checksum


@pytest.mark.parametrize('county,slug', [('부여군','buyeo'), ('청양군','cheongyang')])
def test_real_all_points_against_exhaustive_edge_distances_and_wcc(real_results, county, slug):
    _, results, report = real_results
    # Independent verification: every projected edge is checked, without STRtree.
    graph = ox.load_graphml(ROOT/road.NETWORK_ROOT/slug/'network.graphml')
    metric = ox.project_graph(graph, to_crs='EPSG:5179')
    edges = ox.graph_to_gdfs(metric, nodes=False, fill_edge_geometry=True)
    components = list(nx.weakly_connected_components(graph))
    max_size = max(map(len, components))
    transform = Transformer.from_crs(4326, 5179, always_xy=True)
    for row in results:
        if row['시군명'] != county or row['road_access_status'] != 'matched':
            continue
        p = Point(*transform.transform(float(row['longitude']), float(row['latitude'])))
        edgekey = tuple(int(row[c]) for c in ('road_edge_u','road_edge_v','road_edge_key'))
        assert graph.has_edge(*edgekey)
        line = edges.loc[edgekey, 'geometry']
        all_distances = edges.geometry.distance(p)
        assert float(row['road_distance_m']) == pytest.approx(float(all_distances.min()), abs=1e-6)
        snap = Point(float(row['road_nearest_x_5179']), float(row['road_nearest_y_5179']))
        assert line.distance(snap) < 1e-6
        assert p.distance(snap) == pytest.approx(float(row['road_distance_m']), abs=2e-6)
        rx, ry = transform.transform(float(row['road_nearest_longitude']), float(row['road_nearest_latitude']))
        assert Point(rx, ry).distance(snap) < .00002
        component = next(c for c in components if edgekey[0] in c)
        assert edgekey[1] in component
        assert int(row['road_wcc_node_count']) == len(component)
        assert (row['road_wcc_is_largest']=='true') == (len(component)==max_size)
    assert report['regions'][county]['wcc_count'] == len(components)


def test_real_report_lists_and_map_feature_counts_match_csv(real_results):
    _, rows, report = real_results
    for limit in (50, 100):
        actual = {road.identity(r) for r in rows if r[f'road_review_gt_{limit}m']=='true'}
        listed = {road.identity(r) for r in report[f'outliers_gt_{limit}m']}
        assert actual == listed
    for county, slug in road.REGIONS.items():
        subset = [r for r in rows if r['시군명']==county and r['road_access_status']=='matched']
        document = (ROOT/f'output/road_access/{slug}_map.html').read_text()
        assert document.count('L.circleMarker(') == 2*len(subset)
        assert document.count('L.polyline(') == len(subset)
        assert document.count('"dashArray": "5, 6"') == len(subset)
        assert '실제 시설 진입로 또는 통행 가능성을 확정하지 않습니다.' in document
