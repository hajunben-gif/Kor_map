"""Synthetic regressions for the flood-road CLI, linear overlay and topology."""
import csv
import json
import math
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString, Point, Polygon, box

from scripts.build_flood_depth_scenarios_100y import load_codebook, write_geo_layers
from scripts.build_flood_road_scenarios import (
    Mask, ROOT, build, component_stats, evaluate_facilities, overlay, physical_summary, split_graph,
)
from scripts.connected_access_qa import active_reference_anchors, reference_core
from scripts.prepare_flood_road_od import check_od, routing_view, to_entrance, write_od
from scripts.qa_flood_road_scenarios import audit, check_nested, validate_scenario
from scripts.flood_road_inputs import CODES, FACILITIES, PROFILES, SNAPSHOT, file_sha256, read_flood


def add_edge(graph, u, v, uid, coords, way, bridge=False):
    for node, xy in ((u, coords[0]), (v, coords[-1])):
        graph.add_node(node, x=xy[0], y=xy[1])
    graph.add_edge(u, v, edge_uid=uid, parent_edge_id=uid, osmid=way,
                   ordinals=list(range(len(coords) - 1)), source_node_ids=f'{u},{v}',
                   source_segment_ids=uid, highway='primary',
                   osm_tags=json.dumps({'bridge': 'yes'} if bridge else {}),
                   oneway=bridge, reversed=u > v, geometry=LineString(coords))


class OverlayTests(unittest.TestCase):
    def test_polygon_hole_and_boundary(self):
        geometry = Polygon(box(2, 0, 8, 10).exterior.coords, [box(4, 3, 6, 7).exterior.coords])
        mask = Mask(geometry)
        self.assertEqual(mask.measure(LineString([(0, 5), (10, 5)])), ([(2, 4), (6, 8)], True))
        self.assertEqual(mask.measure(LineString([(0, 0), (10, 0)])), ([], True))
        self.assertEqual(mask.measure(LineString([(0, 2), (2, 0)])), ([], True))

    def test_reverse_direction_partial_block_and_bridge(self):
        graph = nx.MultiDiGraph(profile='synthetic')
        add_edge(graph, 1, 2, 'forward', [(0, 0), (10, 0)], 12)
        add_edge(graph, 2, 1, 'reverse', [(10, 0), (0, 0)], 12)
        add_edge(graph, 3, 4, 'bridge', [(0, 1), (10, 1)], 13, bridge=True)
        measured, cache = overlay(graph, {'S2': Mask(box(1, -2, 3, 2))})
        result, children, impact = split_graph(graph, measured, 'S2')
        self.assertEqual([(r['start_m'], r['end_m']) for r in children
                          if r['edge_uid'] == 'reverse' and r['state'] == 'blocked'], [(7, 9)])
        self.assertEqual(len(impact), 3)
        self.assertEqual(physical_summary(cache, 'S2')['primary']['physical_blocked_m'], 2)
        self.assertEqual(physical_summary(cache, 'S2')['primary']['physical_review_m'], 2)
        self.assertFalse(nx.has_path(result, 1, 2))
        self.assertFalse(nx.has_path(result, 2, 1))
        self.assertTrue(nx.has_path(result, 3, 4))
        self.assertFalse(nx.has_path(result, 4, 3))
        self.assertTrue(all(abs(r['end_m'] - r['start_m'] - r['length_m']) < 1e-9 for r in children))

    def test_self_overlap_preserves_each_visit(self):
        graph = nx.MultiDiGraph(profile='synthetic')
        add_edge(graph, 1, 1, 'loop', [(0, 0), (10, 0), (0, 0)], 12)
        measured, _ = overlay(graph, {'S2': Mask(box(2, -2, 4, 2))})
        _, children, _ = split_graph(graph, measured, 'S2')
        self.assertEqual([(r['start_m'], r['end_m']) for r in children if r['state'] == 'blocked'],
                         [(2, 4), (16, 18)])


class SavedScenarioQATests(unittest.TestCase):
    def test_detects_reopened_physical_measure_with_same_total(self):
        first = {(12, 0): (False, [(2, 4)])}
        second = {(12, 0): (False, [(3, 5)])}
        with self.assertRaisesRegex(ValueError, 'measure missing'):
            check_nested(first, second)

    def test_rejects_reversed_s0_geometry_and_blocked_bridge(self):
        source = nx.MultiDiGraph(profile='synthetic')
        add_edge(source, 1, 2, 'oneway', [(0, 0), (10, 0)], 12, bridge=True)
        measured, _ = overlay(source, {'S1': Mask(box(2, -1, 4, 1))})
        for scenario in ('S0', 'S1'):
            result, trace, _ = split_graph(source, measured, scenario)
            component_stats(result, set(source))
            nodes = [dict(node_id=n, geometry=Point(d['x'], d['y']), wcc=d['wcc'], scc=d['scc'])
                     for n, d in result.nodes(data=True)]
            edges = [dict(d, u=u, v=v) for u, v, d in result.edges(data=True)]
            validate_scenario(source, scenario, trace, nodes, edges)
            if scenario == 'S0':
                edges[0]['geometry'] = edges[0]['geometry'].reverse()
                pattern = 'directed source interval'
            else:
                next(row for row in trace if row['state'] == 'review')['state'] = 'blocked'
                pattern = 'structure review'
            with self.assertRaisesRegex(ValueError, pattern):
                validate_scenario(source, scenario, trace, nodes, edges)

    def test_detects_shared_artificial_junction_before_export(self):
        source = nx.MultiDiGraph(profile='synthetic')
        add_edge(source, 1, 2, 'forward', [(0, 0), (10, 0)], 12)
        add_edge(source, 2, 1, 'reverse', [(10, 0), (0, 0)], 12)
        measured, _ = overlay(source, {'S1': Mask(box(2, -1, 4, 1))})
        result, trace, _ = split_graph(source, measured, 'S1')
        component_stats(result, set(source))
        nodes = [dict(node_id=n, geometry=Point(d['x'], d['y']), wcc=d['wcc'], scc=d['scc'])
                 for n, d in result.nodes(data=True)]
        edges = [dict(d, u=u, v=v) for u, v, d in result.edges(data=True)]
        validate_scenario(source, 'S1', trace, nodes, edges)
        reversed_rows = [r for r in trace if r['edge_uid'] == 'reverse']
        reversed_rows[0]['v'] = trace[0]['v']
        reversed_rows[1]['u'] = trace[0]['v']
        with self.assertRaisesRegex(ValueError, 'shared artificial junction'):
            validate_scenario(source, 'S1', trace, nodes, edges)

    def test_scc_switch_shortens_proximity_but_cannot_shorten_fixed_od(self):
        source = nx.MultiDiGraph(profile='synthetic')
        for u, v, way in [(1, 2, 10), (2, 3, 11), (3, 4, 12), (4, 5, 13)]:
            for first, last in ((u, v), (v, u)):
                add_edge(source, first, last, f'{first}-{last}',
                         [(10 * first, 0), (10 * last, 0)], way)
        anchors, _ = reference_core(source)
        masks = {'S1': Mask(box(33, -1, 37, 1)), 'S2': Mask(box(23, -1, 37, 1).union(box(13, -1, 17, 1)))}
        measured, _ = overlay(source, masks)
        first, second = (split_graph(source, measured, s)[0] for s in ('S1', 'S2'))
        self.assertEqual(active_reference_anchors(first, anchors)[0], {1, 2, 3})
        self.assertEqual(active_reference_anchors(second, anchors)[0], {4, 5})
        for graph in (first, second):
            component_stats(graph, set(source))
        facility = [({}, Point(45, 0))]
        field = 'reference_core_road_distance_m'
        self.assertGreater(next(evaluate_facilities(first, facility, anchors))[field],
                           next(evaluate_facilities(second, facility, anchors))[field])
        before = to_entrance(first, 3)[1]
        after = to_entrance(second, 3).get(1, math.inf)
        self.assertTrue(math.isinf(after))
        check_od(before, after, 1, 3)
        with self.assertRaisesRegex(ValueError, 'Shorter S2 OD'):
            check_od(after, before, 1, 3)

    def test_od_preserves_one_way_and_unknown_entrance(self):
        graph = nx.MultiDiGraph()
        graph.add_edge(1, 2, length_m=10, state='open')
        graph.add_edge(2, 3, length_m=20, state='review')
        self.assertEqual(to_entrance(graph, 1), {})
        self.assertEqual(to_entrance(routing_view(graph, 'open'), 3)[1], 30)
        closed = routing_view(graph, 'closed')
        self.assertEqual(to_entrance(closed, 3), {})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            write_od(output, [1], {('county', '1'): (3, 'checked'),
                                   ('county', '2'): (None, '')},
                     {'S0': graph, 'S1': graph, 'S2': closed})
            with (output / 'unreachable_counts.csv').open(encoding='utf-8-sig') as stream:
                counts = list(csv.DictReader(stream))
            self.assertEqual([row['unreachable_count'] for row in counts], ['0', '0', '1'])
            self.assertEqual([row['entrance_unverified_count'] for row in counts], ['1'] * 3)


class PipelineTests(unittest.TestCase):
    def fixture(self, root):
        gpkg = root / 'flood.gpkg'
        _, codebook, scenarios = load_codebook(ROOT / 'config/flood_seg_code_100y.json')
        depth = {code: box(200010 + 10 * i, 449999, 200020 + 10 * i, 450001)
                 for i, code in enumerate(CODES)}
        areas = gpd.GeoDataFrame(
            [{'source_stem': str(i), 'SEG_CODE': code, 'geometry': geom}
             for i in range(4) for code, geom in depth.items()], crs=5186)
        write_geo_layers(gpkg, areas, codebook, depth, box(200010, 449999, 200060, 450001), scenarios)
        qa = root / 'flood_qa.json'
        qa.write_text(json.dumps({
            'source_crs': 'EPSG:5186', 'source_feature_count': 20, 'sum_original_feature_areas_km2': 0.0004,
            'source_files': [{'stem': str(i), 'feature_area_sum_km2': 0.0001} for i in range(4)],
            'union_footprint_area_km2': 0.0001,
            'scenario_masks': {'S1': {'closure_zone_area_km2': 0.00008},
                               'S2': {'closure_zone_area_km2': 0.0001}},
            'depth_classes': [{'code': code, 'exclusive_priority_area_km2': 0.00002} for code in CODES],
        }))
        road = root / 'road'
        road.mkdir()
        roads = road / 'road_network_profiles.gpkg'
        a, b = (200000, 450000), (200100, 450000)
        for profile in PROFILES:
            gpd.GeoDataFrame({'osmid': [1, 2]}, geometry=[Point(a), Point(b)], crs=5186).to_crs(4326).to_file(
                roads, layer=f'{profile}_simplified_nodes')
            records = [dict(u=u, v=v, key=0, edge_uid=str(u), parent_edge_id=str(u), osmid=12,
                            source_node_ids=f'{u},{v}', raw_segments='[0]', source_segment_ids=str(u),
                            oneway=False, reversed=u > v, highway='primary', osm_tags='{}',
                            geometry=LineString(coords))
                       for u, v, coords in ((1, 2, (a, b)), (2, 1, (b, a)))]
            gpd.GeoDataFrame(records, crs=5186).to_crs(4326).to_file(
                roads, layer=f'{profile}_simplified_edges')
        facilities = root / 'facilities.csv'
        facilities.write_text('시군명,시설일련번호,시설명,latitude,longitude\n' +
                              ''.join(f'Test,{i},Test {i},36.5,127\n' for i in range(146)) + 'Test,146,Missing,,\n',
                              encoding='utf-8')
        (road / 'metadata.json').write_text(json.dumps({
            'status': 'data_validated',
            'source': {'sha256': SNAPSHOT, 'data_timestamp': '2026-11-20T00:00:00Z'},
            'verification': {'source_directed_segments_preserved': True, 'graphml_gpkg_roundtrips': True},
            'output_sha256': {roads.name: file_sha256(roads)},
            'input_sha256': {FACILITIES.as_posix(): file_sha256(facilities)},
        }))
        return Namespace(flood=gpkg, flood_qa=qa, road_artifact=road, facilities=facilities,
                         output=root / 'result', validate_flood_only=False, road_gpkg=None, road_metadata=None)

    def test_pipeline_and_input_mismatches(self):
        with tempfile.TemporaryDirectory() as temp:
            args = self.fixture(Path(temp))
            args.road_gpkg = Path(temp) / 'verified-network.gpkg'
            args.road_metadata = Path(temp) / 'verified-metadata.json'
            (args.road_artifact / 'road_network_profiles.gpkg').rename(args.road_gpkg)
            (args.road_artifact / 'metadata.json').rename(args.road_metadata)
            with patch.dict(PROFILES, {p: (2, 2) for p in PROFILES}):
                build(args)
                args.run = args.output
                report = audit(args)
                comparison = args.run / 'facility_access_comparison.csv'
                content = comparison.read_text(encoding='utf-8-sig')
                content = content.replace('reference_core_road_', 'connected_').replace('nearest_open_road_', 'nearest_')
                comparison.write_text(content, encoding='utf-8-sig')
                validation = args.run / 'validation.json'
                manifest = json.loads(validation.read_bytes())
                manifest['output_sha256'][comparison.name] = file_sha256(comparison)
                validation.write_text(json.dumps(manifest))
                self.assertEqual(audit(args), report)
            self.assertEqual(report['status'], 'passed')
            stats = report['profiles']['relief_vehicle_candidate']['scenarios']['S2']
            self.assertAlmostEqual(stats['physical_blocked_m'], 50, places=5)
            with (args.output / 'scenario_comparison.csv').open(encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 9)
            self.assertEqual([row['blocked_directed_parent_edges'] for row in rows[:3]], ['0', '2', '2'])
            self.assertAlmostEqual(float(rows[1]['physical_blocked_m']), 40, places=5)
            self.assertAlmostEqual(float(rows[2]['directed_blocked_m']), 100, places=5)
            self.assertEqual(len(gpd.list_layers(args.output / 'road_scenarios.gpkg')), 18)
            self.assertTrue((args.output / 'flood_road_map.html').is_file())
            self.assertTrue((args.output / 'report_ko.md').is_file())
            with self.assertRaisesRegex(ValueError, 'Output already exists'):
                build(args)
            qa = json.loads(args.flood_qa.read_text())
            qa['union_footprint_area_km2'] *= 2
            args.flood_qa.write_text(json.dumps(qa))
            with self.assertRaisesRegex(ValueError, 'Area mismatch'):
                read_flood(args.flood, args.flood_qa)
