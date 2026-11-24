"""Check saved flood scenario topology and explain changes in proximity metrics."""
import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path

import geopandas as gpd
import networkx as nx
import shapely
from shapely.geometry import Point
from shapely.ops import substring

from scripts.build_flood_road_scenarios import read_facilities
from scripts.connected_access_qa import active_reference_anchors, reference_core
from scripts.flood_road_inputs import (
    CRS, FACILITIES, PROFILES, file_sha256, read_profile, read_road_metadata, require,
)

SCENARIOS = ('S0', 'S1', 'S2')
TOL = 1e-6
TRACE_FIELDS = (
    'edge_uid', 'parent_edge_id', 'source_segment_ids', 'source_node_ids', 'osmid',
    'oneway', 'reversed', 'highway', 'osm_tags',
)


def csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def as_bool(value):
    require(str(value) in ('True', 'False', '0', '1'), f'Invalid boolean: {value}')
    return str(value) in ('True', '1')


def normalize(row):
    row = dict(row)
    for field in ('u', 'v', 'original_u', 'original_v', 'original_key', 'osmid'):
        row[field] = int(row[field])
    for field in ('start_m', 'end_m', 'length_m'):
        row[field] = float(row[field])
        require(math.isfinite(row[field]), f'{row["child_id"]}: invalid {field}')
    for field in ('oneway', 'reversed'):
        row[field] = as_bool(row[field])
    return row


def unique(rows, field):
    result = {row[field]: row for row in rows}
    require(len(result) == len(rows), f'Duplicate {field}')
    return result


def read_saved(gpkg, name, kind):
    frame = gpd.read_file(gpkg, layer=f'{name}_{kind}')
    require(frame.crs.to_epsg() == CRS, f'{name}: unexpected {kind} CRS')
    return frame.to_dict('records')


def check_components(graph, original):
    stats = dict(active_nodes=len(graph), active_directed_edges=graph.number_of_edges(),
                 original_nodes_retained=len(original & graph.nodes),
                 isolated_original_nodes_removed=len(original - graph.nodes))
    for tag, components in (('wcc', nx.weakly_connected_components),
                            ('scc', nx.strongly_connected_components)):
        groups = list(components(graph))
        labels = [set(graph.nodes[node][tag] for node in group) for group in groups]
        require(all(len(label) == 1 for label in labels)
                and len(set.union(set(), *labels)) == len(groups), f'{tag}: incorrect component partition')
        stats[f'{tag}_count'] = len(groups)
        stats[f'{tag}_count_with_original_isolates'] = len(groups) + stats['isolated_original_nodes_removed']
        stats[f'largest_{tag}_original_nodes'] = max((len(g & original) for g in groups), default=0)
    return stats


def review_structure(tags):
    tags = json.loads(tags)
    try:
        elevated = float(tags.get('layer', 0)) > 0
    except ValueError:
        elevated = True
    return (elevated or tags.get('man_made') == 'viaduct' or tags.get('location') == 'elevated'
            or tags.get('bridge', 'no').lower() not in ('', 'no', 'false', '0'))


def merge(spans):
    result = []
    for start, end in sorted(spans):
        if result and start <= result[-1][1] + TOL:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def covered(subset, superset):
    j = 0
    for start, end in subset:
        while j < len(superset) and superset[j][1] < start - TOL:
            j += 1
        if j == len(superset) or superset[j][0] > start + TOL or superset[j][1] < end - TOL:
            return False
    return True


def physical_trace(source, parts):
    """Project directional interval measures onto canonical (way, ordinal) measures."""
    physical = defaultdict(list)
    for _, _, data in source.edges(data=True):
        coords = list(data['geometry'].coords)
        osm_nodes = list(map(int, data['source_node_ids'].split(',')))
        wet = [r for r in parts[data['edge_uid']] if r['state'] != 'open']
        offset = j = 0
        for ordinal, (a, b), (first, last) in zip(
                data['ordinals'], pairwise(coords), pairwise(osm_nodes), strict=True):
            length = math.dist(a, b)
            spans = []
            while j < len(wet) and wet[j]['end_m'] <= offset:
                j += 1
            k = j
            while k < len(wet) and wet[k]['start_m'] < offset + length:
                row = wet[k]
                lo = max(0.0, row['start_m'] - offset)
                hi = min(length, row['end_m'] - offset)
                if hi > lo:
                    spans.append((lo, hi) if (a, first) <= (b, last) else (length - hi, length - lo))
                k += 1
            identity = (data['osmid'], ordinal)
            endpoints = tuple(sorted(((a, first), (b, last))))
            physical[identity].append((endpoints, review_structure(data['osm_tags']), merge(spans)))
            offset += length
    result = {}
    for identity, records in physical.items():
        endpoints, review, spans = records[0]
        for other_endpoints, other_review, other in records[1:]:
            require(endpoints == other_endpoints and review == other_review
                    and covered(spans, other) and covered(other, spans),
                    f'{identity}: inconsistent physical segment directions or review')
        if spans:
            result[identity] = (review, spans)
    return result


def check_nested(previous, current):
    for identity, (review, spans) in previous.items():
        require(identity in current and current[identity][0] == review
                and covered(spans, current[identity][1]),
                f'{identity}: S1 blocked/review measure missing in S2')


def validate_scenario(source, scenario, trace, nodes, edges):
    parts = defaultdict(list)
    for row in unique([normalize(row) for row in trace], 'child_id').values():
        parts[row['edge_uid']].append(row)
    saved = unique([normalize(row) for row in edges], 'child_id')
    nodes = unique(nodes, 'node_id')
    originals = set(source)
    endpoints = set()
    cuts = set()
    open_ids = set()
    parents = {d['edge_uid']: (u, v, key, d) for u, v, key, d in source.edges(keys=True, data=True)}
    require(parts.keys() == parents.keys(), 'Missing or unknown parent edge')
    graph = nx.MultiDiGraph()
    for uid, (u, v, key, data) in parents.items():
        line = data['geometry']
        rows = sorted(parts[uid], key=lambda row: (row['start_m'], row['end_m']))
        parts[uid] = rows
        previous_end, previous_node = 0.0, u
        require(scenario != 'S0' or len(rows) == 1, f'{uid}: split in S0')
        for index, row in enumerate(rows):
            child = row['child_id']
            start, end = row['start_m'], row['end_m']
            identity = (row['original_u'], row['original_v'], row['original_key'])
            require(child == f'{uid}:{index}' and identity == (u, v, key)
                    and all(row[f] == data[f] for f in TRACE_FIELDS), f'{child}: source identity mismatch')
            require(abs(start - previous_end) <= TOL and 0 <= start <= end <= line.length + TOL
                    and abs(end - start - row['length_m']) <= TOL
                    and (end > start or line.length == 0), f'{child}: invalid length partition')
            require(row['u'] == previous_node, f'{child}: discontinuous directed chain')
            target = row['v']
            if index == len(rows) - 1:
                require(target == v and abs(end - line.length) <= TOL, f'{child}: wrong parent terminus')
            else:
                require(target not in originals and target not in cuts, f'{child}: shared artificial junction')
                cuts.add(target)
            previous_end, previous_node = end, target
            expected_wet = 'review' if review_structure(data['osm_tags']) else 'blocked'
            require(row['state'] == 'open' or (scenario != 'S0' and row['state'] == expected_wet),
                    f'{child}: invalid closure or structure review')
            if row['state'] == 'blocked':
                continue
            open_ids.add(child)
            actual = saved.get(child)
            require(actual is not None and all(actual[f] == row[f] for f in row),
                    f'{child}: open edge differs from trace')
            shape = actual['geometry']
            expected = line if len(rows) == 1 else substring(line, start, end)
            require(shape.geom_type == 'LineString' and not shape.is_empty
                    and shape.equals_exact(expected, TOL)
                    and abs(shape.length - row['length_m']) <= TOL,
                    f'{child}: geometry differs from directed source interval')
            for node, xy in ((row['u'], shape.coords[0]), (target, shape.coords[-1])):
                require(node in nodes and nodes[node]['geometry'].geom_type == 'Point'
                        and nodes[node]['geometry'].distance(Point(xy)) <= 1e-5,
                        f'{child}: wrong endpoint {node}')
                endpoints.add(node)
            graph.add_edge(row['u'], target, key=child, **{
                field: actual[field] for field in ('highway', 'osmid', 'geometry', 'length_m', 'state')
            })
    require(open_ids == saved.keys() and endpoints == nodes.keys(), 'Unexpected open edges or isolated nodes')
    require(scenario != 'S0' or endpoints == originals, 'S0 original node set differs')
    graph.add_nodes_from((node, dict(wcc=row['wcc'], scc=row['scc'])) for node, row in nodes.items())
    physical = physical_trace(source, parts)
    stats = check_components(graph, originals)
    for state, review in (('blocked', False), ('review', True)):
        stats[f'physical_{state}_m'] = sum(
            hi - lo for flag, spans in physical.values() if flag == review for lo, hi in spans)
    stats['trace_states'] = dict(Counter(r['state'] for rows in parts.values() for r in rows))
    return graph, physical, stats


def normalized_proximity(rows):
    for row in rows:
        yield {**row, **{
            f'{old}_{suffix}': row[f'{new}_{suffix}']
            for old, new in (('nearest', 'nearest_open_road'), ('connected', 'reference_core_road'))
            for suffix in ('distance_m', 'child_id')
            if f'{new}_{suffix}' in row
        }}


def check_proximity(graph, selected, facilities, rows):
    targets = {(r['시군명'], r['시설일련번호']): r for r in rows}
    require(len(targets) == len(rows) == len(facilities), 'Unexpected facility rows')
    core_label = graph.nodes[min(selected)]['scc'] if selected else None
    for label in ('nearest', 'connected'):
        edges = {
            child: data['geometry'] for u, v, child, data in graph.edges(keys=True, data=True)
            if data['length_m'] > 1e-7 and (label == 'nearest' or (
                core_label is not None and graph.nodes[u]['scc'] == graph.nodes[v]['scc'] == core_label
                and data['highway'] not in {'motorway', 'motorway_link', 'trunk', 'trunk_link'}))
        }
        tree = shapely.STRtree(list(edges.values()))
        if edges:
            _, distances = tree.query_nearest([point for _, point in facilities],
                                             return_distance=True, all_matches=False)
        else:
            distances = [math.inf] * len(facilities)
        for (identity, point), expected in zip(facilities, distances, strict=True):
            key = (identity['시군명'], identity['시설일련번호'])
            row = targets[key]
            child, distance = row[f'{label}_child_id'], row[f'{label}_distance_m']
            valid = (not child and not distance) if math.isinf(expected) else (
                child in edges and bool(distance) and abs(float(distance) - expected) <= TOL
                and abs(edges[child].distance(point) - expected) <= TOL)
            require(valid, f'{key}: saved {label} proximity differs from audited network')


def compare_proximity(rows, profile, selected):
    """Explain saved decreases using the actual selected reference anchor sets."""
    by_scenario = defaultdict(list)
    for row in rows:
        if row['profile'] == profile:
            by_scenario[row['scenario']].append(row)
    for scenario in SCENARIOS:
        keys = [(r['시군명'], r['시설일련번호']) for r in by_scenario[scenario]]
        require(len(keys) == len(set(keys)) == 146, f'{profile}/{scenario}: incorrect facility IDs')
    s1, s2 = ({(r['시군명'], r['시설일련번호']): r for r in by_scenario[s]} for s in ('S1', 'S2'))
    require(s1.keys() == s2.keys(), f'{profile}: facilities changed across scenarios')
    decreases = []
    same_branch = bool(selected['S1'] & selected['S2'])
    for key, row in s1.items():
        later = s2[key]
        for field in ('nearest_distance_m', 'connected_distance_m'):
            before = float(row[field]) if row[field] else math.inf
            after = float(later[field]) if later[field] else math.inf
            if after + TOL >= before:
                continue
            require(field != 'nearest_distance_m' and not same_branch,
                    f'{profile}: unexpected {field} decrease for {key}')
            decreases.append(dict(county=key[0], facility_id=key[1],
                                  s1_m=before if math.isfinite(before) else None, s2_m=after,
                                  s1_child=row['connected_child_id'], s2_child=later['connected_child_id']))
    return decreases


def audit(args, on_graph=None):
    manifest = json.loads((args.run / 'validation.json').read_bytes())
    require(manifest['status'] == 'passed', 'Original run did not pass')
    road, metadata = read_road_metadata(args.road_gpkg, args.road_metadata)
    require(all(metadata[field] == manifest['roads'][field] for field in ('sha256', 'metadata_sha256')),
            'Original road artifact or metadata differs from run')
    require(file_sha256(args.facilities) == manifest['facility_sha256']
            == metadata['verified_facility_sha256'], 'Facility input differs from run')
    gpkg = args.run / 'road_scenarios.gpkg'
    proximity = args.run / 'facility_access_comparison.csv'
    names = [gpkg, proximity, *(args.run / f'{p}_{s.lower()}_{suffix}.csv'
                                for p in PROFILES for s in SCENARIOS
                                for suffix in ('edge_trace', 'node_components'))]
    for path in names:
        require(file_sha256(path) == manifest['output_sha256'][path.name], f'Run checksum mismatch: {path.name}')
    with sqlite3.connect(gpkg.resolve().as_uri() + '?mode=ro', uri=True) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'GeoPackage integrity failure')
    ordinary = read_profile(road, 'drive_reference')
    anchors, _ = reference_core(ordinary)
    require(anchors == set(manifest['baseline_reference_core']['anchor_osm_ids']), 'Baseline anchors differ')
    saved_stats = {(r['profile'], r['scenario']): r for r in manifest['scenarios']}
    proximity_rows = list(normalized_proximity(csv_rows(proximity)))
    facilities = read_facilities(args.facilities)
    result = {}
    for profile in PROFILES:
        source = ordinary if profile == 'drive_reference' else read_profile(road, profile)
        selected, stats, physical, partitions = {}, {}, {}, {}
        for scenario in SCENARIOS:
            name = f'{profile}_{scenario.lower()}'
            graph, physical[scenario], stats[scenario] = validate_scenario(
                source, scenario, csv_rows(args.run / f'{name}_edge_trace.csv'),
                read_saved(gpkg, name, 'nodes'), read_saved(gpkg, name, 'edges'))
            partitions[scenario] = {n: (graph.nodes[n]['wcc'], graph.nodes[n]['scc'])
                                    for n in source if n in graph}
            origin = partitions['S0']
            current = partitions[scenario]
            archived = unique(csv_rows(args.run / f'{name}_node_components.csv'), 'osm_node')
            require({int(n) for n in archived} == set(origin), f'{name}: incorrect node mapping keys')
            for n, pair in origin.items():
                expected = (*pair, *current.get(n, ('', '')))
                row = archived[str(n)]
                fields = ('s0_wcc', 's0_scc', 'scenario_wcc', 'scenario_scc')
                require(tuple(row[f] for f in fields) == tuple(map(str, expected)),
                        f'{name}: incorrect node mapping for {n}')
            selected[scenario], selection = active_reference_anchors(graph, anchors)
            stats[scenario]['reference_selection'] = selection
            check_proximity(graph, selected[scenario], facilities, [
                row for row in proximity_rows if (row['profile'], row['scenario']) == (profile, scenario)
            ])
            if on_graph is not None:
                on_graph(profile, scenario, source, graph)
            expected = saved_stats[profile, scenario]
            for field in stats[scenario].keys() & expected.keys():
                tolerance = TOL * max(1, len(physical[scenario])) if field == 'physical_blocked_m' else 0
                require(abs(stats[scenario][field] - expected[field]) <= tolerance,
                        f'{name}: {field} differs from manifest')
        check_nested(physical['S1'], physical['S2'])
        for earlier, later in zip(SCENARIOS, SCENARIOS[1:]):
            before, after = partitions[earlier], partitions[later]
            require(after.keys() <= before.keys(), f'{profile}: original node became active in {later}')
            for index, tag in enumerate(('wcc', 'scc')):
                membership = defaultdict(set)
                for node in after:
                    membership[after[node][index]].add(before[node][index])
                require(all(len(group) == 1 for group in membership.values()),
                        f'{profile}: original {tag} components merged in {later}')
        result[profile] = dict(scenarios=stats,
                               shared_s1_s2_anchors=len(selected['S1'] & selected['S2']),
                               anchor_proximity_decreases=compare_proximity(proximity_rows, profile, selected))
    return dict(status='passed', profiles=result, facility_sha256=manifest['facility_sha256'],
                run_gpkg_sha256=manifest['output_sha256'][gpkg.name])


def add_input_arguments(parser):
    for name in ('run', 'road-gpkg', 'road-metadata', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--facilities', type=Path, default=FACILITIES)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_input_arguments(parser)
    args = parser.parse_args()
    require(not args.output.resolve().is_relative_to(args.run.resolve()), 'Output must be outside the run')
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        result = audit(args)
    except Exception as error:
        (args.output / 'qa.json').write_text(json.dumps(dict(status='failed', error=str(error))),
                                             encoding='utf-8')
        raise
    (args.output / 'qa.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n',
                                         encoding='utf-8')


if __name__ == '__main__':
    main()
