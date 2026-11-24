import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import networkx as nx
from pyproj import Transformer
import shapely
from shapely.geometry import LineString, Point, box

from scripts.facility_access_scenarios import (
    Span, arrival_at_measure, check_trace_nesting, connector_in_mask, point_in_mask, read_trace,
)
from scripts.finalize_facility_access_points import Position, ProjectionCheck, directed_positions, scenario_rows


def span(i, u, v, start, end, state='open', uid='road'):
    return Span(f'{uid}:{i}', u, v, start, end, end - start, state)


class ArrivalTests(unittest.TestCase):
    def test_partial_arrival_is_not_a_transit_edge(self):
        parts = [span(0, 1, -1, 0, 25), span(1, -1, -2, 25, 70, 'blocked'),
                 span(2, -2, 2, 70, 100)]
        arrival = arrival_at_measure(parts, 75)
        self.assertEqual(arrival['point_state'], 'open')
        self.assertEqual(arrival['arrival_node_id'], -2)
        self.assertEqual((arrival['partial_start_m'], arrival['partial_end_m']), (70, 75))
        self.assertEqual(arrival['arrival_length_m'], 5)
        self.assertEqual(arrival['arrival_child_length_m'], 30)
        graph = nx.DiGraph((p.u, p.v) for p in parts if p.state == 'open')
        sink = 'demand:city:test'
        graph.add_edge(arrival['arrival_node_id'], sink, length_m=arrival['arrival_length_m'])
        self.assertFalse(nx.has_path(graph, 1, sink))
        self.assertEqual(nx.shortest_path_length(graph, -2, sink, weight='length_m'), 5)
        self.assertEqual(graph.out_degree(sink), 0)

    def test_blocked_boundary_and_existing_endpoint(self):
        parts = [span(0, 1, -1, 0, 25), span(1, -1, 2, 25, 100, 'blocked')]
        hit = arrival_at_measure(parts, 25)
        self.assertTrue(hit['touches_internal_cut'])
        self.assertEqual(hit['point_state'], 'blocked')
        self.assertIsNone(hit['arrival_node_id'])
        self.assertFalse(hit['arrival_allowed_review_open'])
        at_start = arrival_at_measure(parts, 0)
        self.assertEqual(at_start['arrival_node_id'], 1)
        self.assertEqual(at_start['arrival_length_m'], 0)
        self.assertTrue(at_start['uses_existing_scenario_node'])

    def test_review_boundary_keeps_policy_visible(self):
        parts = [span(0, 1, -1, 0, 25), span(1, -1, 2, 25, 50, 'review')]
        hit = arrival_at_measure(parts, 25)
        self.assertEqual(hit['arrival_node_id'], -1)
        self.assertEqual(hit['point_state'], 'review')
        self.assertFalse(hit['arrival_allowed_review_closed'])
        self.assertTrue(hit['arrival_allowed_review_open'])

    def test_trace_nesting_refuses_reopened_wet_length(self):
        first = dict(road=[span(0, 1, -1, 0, 20), span(1, -1, 2, 20, 50, 'blocked')])
        later = dict(road=[span(0, 1, -1, 0, 15), span(1, -1, 2, 15, 50, 'blocked')])
        check_trace_nesting(first, later)
        with self.assertRaisesRegex(ValueError, 'reopens'):
            check_trace_nesting(later, first)

    def test_fixed_scenario_position_records_blocking(self):
        parent = dict(child_id='road:0', edge_uid='road', u=1, v=2,
                      start_m=0, end_m=100, length_m=100, state='open')
        row = dict(access_point_id='access:city:1', county='city', facility_id='1',
                   facility_name='test', final_projection_latitude='36',
                   final_projection_longitude='127')
        traces = dict(S1=dict(road=[span(0, 1, 2, 0, 100)]),
                      S2=dict(road=[span(0, 1, -1, 0, 20), span(1, -1, 2, 20, 100, 'blocked')]))
        status, links = scenario_rows(row, [(parent, 60)], traces)
        self.assertEqual([s['point_state'] for s in status], ['open', 'open', 'blocked'])
        self.assertEqual({s['final_projection_latitude'] for s in status}, {'36'})
        self.assertEqual({s['final_projection_longitude'] for s in status}, {'127'})
        self.assertEqual({s['s0_edge_measure_m'] for s in links}, {60})
        self.assertEqual([s['arrival_length_m'] for s in links], [60, 60, None])


class RoadTraceTests(unittest.TestCase):
    def test_repeat_cut_id_is_invalid_even_at_same_coordinate(self):
        parents = {
            uid: dict(edge_uid=uid, parent_edge_id=uid, source_node_ids=f'{start},{end}',
                      source_segment_ids=uid, osmid=start, highway='residential', osm_tags='{}',
                      original_u=start, original_v=end, original_key=0, u=start, v=end,
                      oneway=True, reversed=False, geometry=LineString(((0, 0), (10, 0))))
            for uid, start, end in (('one', 1, 2), ('two', 3, 4))
        }
        rows = []
        for uid, parent in parents.items():
            metadata = {k: v for k, v in parent.items() if k != 'geometry'}
            first = dict(metadata, child_id=f'{uid}:0', v=-1, start_m=0, end_m=5,
                         length_m=5, state='open')
            last = dict(metadata, child_id=f'{uid}:1', u=-1, start_m=5, end_m=10,
                        length_m=5, state='blocked')
            rows.extend((first, last))
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'trace.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, 'shared artificial junction'):
                read_trace(path, parents, {1, 2, 3, 4})

    def test_reverse_position_uses_only_matching_osm_chain(self):
        road = dict(edge_uid='a', osmid=1, source_node_ids='1,2,3', oneway=False, reversed=False,
                    geometry=LineString([(0, 0), (5, 0), (10, 0)]))
        reverse = dict(road, edge_uid='b', source_node_ids='3,2,1', reversed=True,
                       geometry=LineString([(10, 0), (5, 0), (0, 0)]))
        co_located = dict(reverse, edge_uid='c', source_node_ids='7,8,9')
        position = Position(road, 2, Point(2, 0), '36', '127')
        approaches = directed_positions(position, {1: [road, reverse, co_located]}, {'a', 'b', 'c'})
        self.assertEqual([(p['edge_uid'], m) for p, m in approaches], [('a', 2), ('b', 8)])
        with self.assertRaisesRegex(ValueError, 'missing reverse'):
            directed_positions(position, {1: [road, co_located]}, {'a', 'c'})

    def test_revised_coordinate_must_match_the_recorded_measure(self):
        start = Point(200000, 450000)
        end = Point(200100, 450000)
        edge = dict(edge_uid='road', osmid=1, highway='residential', geometry=LineString([start, end]))
        check = ProjectionCheck({'road': edge}, {'road'})
        transformer = Transformer.from_crs(5186, 4326, always_xy=True)
        mapped_lon, mapped_lat = transformer.transform(200040, 450000)
        clicked_lon, clicked_lat = transformer.transform(200040, 450010)
        row = dict(revised_s0_edge_uid='road', revised_s0_edge_measure_m='40',
                   revised_osm_way_id='1', revised_highway='residential',
                   revised_projection_latitude=mapped_lat, revised_projection_longitude=mapped_lon,
                   revised_projection_distance_m='40', clicked_latitude=clicked_lat,
                   clicked_longitude=clicked_lon, click_to_revised_projection_m='10',
                   nearest_clicked_osm_way_id='1', nearest_clicked_road_eligible='true')
        point = check.validate(row, start, 'site', revised=True)
        self.assertEqual(point.measure_m, 40)
        row['revised_s0_edge_measure_m'] = '50'
        with self.assertRaisesRegex(ValueError, 'recorded directed measure'):
            check.validate(row, start, 'site', revised=True)


class FloodOverlayTests(unittest.TestCase):
    def test_boundary_and_positive_overlap_are_separate(self):
        tree = shapely.STRtree([box(0, 0, 4, 4)])
        self.assertEqual(point_in_mask(Point(3, 2), tree), 'inside')
        self.assertEqual(point_in_mask(Point(0, 2), tree), 'boundary')
        self.assertEqual(point_in_mask(Point(-1, 2), tree), 'outside')
        self.assertEqual(connector_in_mask(Point(-1, 0), Point(5, 0), tree), ('boundary_contact', 0))
        self.assertEqual(connector_in_mask(Point(-1, 2), Point(5, 2), tree), ('partially_overlapped', 4))
        self.assertEqual(connector_in_mask(Point(1, 2), Point(3, 2), tree), ('fully_overlapped', 2))
