"""Finalize the fixed S0 assignment and prepare directional demand arrival links."""

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import tempfile

from pyproj import Transformer
import shapely
from shapely.geometry import Point

from scripts.build_facility_entrance_candidates import RoadIndex
from scripts.build_flood_road_scenarios import write_csv, write_json
from scripts.facility_access_scenarios import (
    PROFILE, S0_LAYER, STATE_RANK, WET_SCENARIOS, Span, arrival_at_measure,
    check_trace_nesting, connector_in_mask, point_in_mask, read_masks, read_trace, s0_edges_match,
)
from scripts.flood_road_inputs import CRS, PROFILES, file_sha256, require
from scripts.qa_flood_road_scenarios import SCENARIOS, TOL, normalize, read_saved, unique

# Revised longitude/latitude and distances come from a finite-precision browser export.
CSV_TOL_M = 0.002
FACILITIES = 146
DECISIONS = Counter(keep=26, revise=4)
GROUPS = Counter(extreme_outlier=6, upper_outlier=10, spatial=2, audit_sample=12)
IDENTITY = ('facility_name', 'address')
AUTO_FIELDS = ('s0_edge_uid', 'osm_way_id', 'highway', 's0_edge_measure_m',
               'road_projection_distance_m', 'projection_latitude', 'projection_longitude')
REVIEW_ORIGINAL = dict(
    original_s0_edge_uid='s0_edge_uid', original_osm_way_id='osm_way_id',
    original_highway='highway', original_projection_distance_m='road_projection_distance_m',
)
REVISED = (
    'clicked_latitude', 'clicked_longitude', 'revised_projection_latitude',
    'revised_projection_longitude', 'revised_projection_distance_m', 'click_to_revised_projection_m',
    'revised_highway', 'revised_osm_way_id', 'revised_s0_edge_uid', 'revised_s0_edge_measure_m',
    'nearest_clicked_osm_way_id', 'nearest_clicked_road_eligible', 'review_warnings',
)
TO_METRIC = Transformer.from_crs(4326, CRS, always_xy=True)


@dataclass(frozen=True, slots=True)
class Position:
    edge: dict
    measure_m: float
    point: Point
    latitude: str
    longitude: str


def csv_records(path, fields):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        require(len(set(header)) == len(header) and set(fields) <= set(header),
                f'{path.name}: missing or duplicate CSV columns')
        rows = list(reader)
    require(all(None not in row and None not in row.values() for row in rows),
            f'{path.name}: invalid CSV row width')
    return rows


def by_facility(rows):
    result = {(r['county'], r['facility_id']): r for r in rows}
    require(len(result) == len(rows), 'Duplicate (county, facility_id)')
    return result


def finite(value, context):
    result = float(value)
    require(math.isfinite(result), f'{context}: non-finite numeric field')
    return result


def coord(row, prefix):
    lat = finite(row[f'{prefix}_latitude'], prefix)
    lon = finite(row[f'{prefix}_longitude'], prefix)
    return Point(TO_METRIC.transform(lon, lat, errcheck=True))


def source_facilities(path):
    rows = csv_records(path, ('시군명', '시설일련번호', '시설명', '상세주소', 'latitude', 'longitude'))
    geocoded = [r for r in rows if r['latitude'] or r['longitude']]
    require(len(geocoded) == FACILITIES, f'Expected {FACILITIES} geocoded source facilities')
    return by_facility([dict(county=r['시군명'], facility_id=r['시설일련번호'], facility_name=r['시설명'],
                             address=r['상세주소'], facility_latitude=r['latitude'],
                             facility_longitude=r['longitude']) for r in geocoded])


def check_reference(row, base, key, coordinate_prefix='projection'):
    require(all(row[k] == base[k] for k in IDENTITY)
            and coord(row, 'facility').distance(coord(base, 'facility')) <= CSV_TOL_M
            and coord(row, coordinate_prefix).distance(coord(base, 'projection')) <= CSV_TOL_M,
            f'{key}: facility or original projection changed')


def read_cohort(projection_dir, automatic):
    targets = {}
    for kind in ('review_queue', 'audit_sample'):
        path = projection_dir / f'facility_projection_{kind}.csv'
        fields = (*AUTO_FIELDS, *IDENTITY, 'county', 'facility_id', 'facility_latitude',
                  'facility_longitude', 'distance_class', 'review_reasons')
        if kind == 'audit_sample':
            fields += ('distance_tercile',)
        for key, row in by_facility(csv_records(path, fields)).items():
            require(key in automatic and key not in targets, f'{key}: invalid or duplicate review cohort')
            base = automatic[key]
            check_reference(row, base, key)
            require(all(row[field] == base[field] for field in AUTO_FIELDS),
                    f'{key}: cohort projection differs from the automatic assignments')
            group = row['distance_class'] if row['distance_class'] != 'non_outlier' else 'spatial'
            reason = row['review_reasons']
            if kind == 'audit_sample':
                require(row['distance_class'] == 'non_outlier' and not reason
                        and row['distance_tercile'] in ('1', '2', '3'), f'{key}: invalid audit sample')
                group, reason = 'audit_sample', 'audit_sample_tercile_' + row['distance_tercile']
            targets[key] = group, reason
    require(Counter(group for group, _ in targets.values()) == GROUPS, 'Review cohort counts differ')
    return targets


def read_reviews(path, automatic, cohort):
    rows = by_facility(csv_records(path, (
        'county', 'facility_id', *IDENTITY, 'facility_latitude', 'facility_longitude',
        'original_projection_latitude', 'original_projection_longitude', *REVIEW_ORIGINAL,
        'review_group', 'selection_reason', 'review_state', 'review_decision', 'review_reason',
        'review_notes', 'reviewed_at_utc', *REVISED,
    )))
    require(rows.keys() == cohort.keys(), 'The manual CSV must cover the original 30 review facilities exactly')
    require(Counter(r['review_decision'] for r in rows.values()) == DECISIONS,
            'Expected 26 keep / 4 revise decisions')
    for key, r in rows.items():
        base = automatic[key]
        check_reference(r, base, key, 'original_projection')
        require(r['review_state'] == 'saved' and (r['review_group'], r['selection_reason']) == cohort[key]
                and datetime.fromisoformat(r['reviewed_at_utc']).utcoffset() is not None,
                f'{key}: incomplete saved review')
        require(all(r[k] == base[v] for k, v in REVIEW_ORIGINAL.items()
                    if k != 'original_projection_distance_m')
                and abs(finite(r['original_projection_distance_m'], key)
                        - finite(base['road_projection_distance_m'], key)) <= CSV_TOL_M,
                f'{key}: original review attributes changed')
        if r['review_decision'] == 'keep':
            require(not any(r[k].strip() for k in REVISED), f'{key}: keep decision contains a revision')
        else:
            require(bool(r['review_reason'].strip()), f'{key}: entrance refinement needs a reason')
    return rows


def attachment_eligible(nodes, edges, node_tags):
    index = RoadIndex(list(nodes.values()), list(edges.values()), node_tags)
    return {arc['edge_uid'] for arcs in index.arcs.values() for arc in arcs if arc['eligible']
            and edges[arc['edge_uid']]['geometry'].length > 0}


class ProjectionCheck:
    def __init__(self, edges, eligible):
        require(eligible, 'No eligible S0 attachment edge')
        self.edges = edges
        self.eligible = eligible
        self.nearest_tree = shapely.STRtree([edges[i]['geometry'] for i in sorted(eligible)])
        self.road_ids = [uid for uid, row in edges.items() if row['geometry'].length > 0]
        self.all_tree = shapely.STRtree([edges[i]['geometry'] for i in self.road_ids])

    def validate(self, row, facility, key, *, revised=False):
        uid = row['revised_s0_edge_uid' if revised else 's0_edge_uid']
        require(uid in self.eligible, f'{key}: attachment edge is not eligible in S0')
        prefix = 'revised_' if revised else ''
        coordinate_prefix = 'revised_projection' if revised else 'projection'
        edge = self.edges[uid]
        line = edge['geometry']
        measure = finite(row[prefix + 's0_edge_measure_m'], key)
        require(0 <= measure <= line.length + TOL, f'{key}: position outside S0 edge')
        # Permit sub-micrometre CSV rounding at the parent endpoint.
        measure = min(measure, line.length)
        point = line.interpolate(measure)
        require(str(edge['osmid']) == row[prefix + 'osm_way_id']
                and edge['highway'] == row[prefix + 'highway']
                and point.distance(coord(row, coordinate_prefix)) <= CSV_TOL_M,
                f'{key}: coordinates or road IDs do not match the recorded directed measure')
        reported = row['revised_projection_distance_m' if revised else 'road_projection_distance_m']
        require(abs(point.distance(facility) - finite(reported, key)) <= CSV_TOL_M,
                f'{key}: straight-line distance differs from CSV')
        origin = coord(row, 'clicked') if revised else facility
        _, distances = self.nearest_tree.query_nearest(origin, return_distance=True)
        require(abs(point.distance(origin) - distances[0]) <= CSV_TOL_M,
                f'{key}: recorded position is not the nearest eligible projection')
        if revised:
            click_gap = point.distance(origin)
            require(origin.distance(facility) <= 750 + CSV_TOL_M and click_gap <= 250 + CSV_TOL_M
                    and abs(click_gap - finite(row['click_to_revised_projection_m'], key)) <= CSV_TOL_M,
                    f'{key}: click-to-road validation failed')
            _, raw_distance = self.all_tree.query_nearest(origin, return_distance=True)
            reported_flag = row['nearest_clicked_road_eligible'].lower()
            require(reported_flag in ('true', 'false'), f'{key}: invalid click-side eligibility flag')
            # Preserve an equally near raw road if the browser selected it at a tie.
            candidates = self.all_tree.query(origin, predicate='dwithin', distance=float(raw_distance[0]) + CSV_TOL_M)
            require(any(str(self.edges[self.road_ids[i]]['osmid']) == row['nearest_clicked_osm_way_id']
                        and (self.road_ids[i] in self.eligible) == (reported_flag == 'true')
                        for i in candidates), f'{key}: nearest clicked road disagrees')
        return Position(edge, measure, point, row[coordinate_prefix + '_latitude'],
                        row[coordinate_prefix + '_longitude'])


def directed_positions(position, by_way, eligible):
    """Pair an existing reverse direction only when source way, node sequence and geometry match."""
    main = position.edge
    line = main['geometry']
    nodes = tuple(reversed(main['source_node_ids'].split(',')))
    coords = tuple(reversed(line.coords))
    reverse = [edge for edge in by_way[main['osmid']] if edge['edge_uid'] != main['edge_uid']
               and tuple(edge['source_node_ids'].split(',')) == nodes
               and tuple(edge['geometry'].coords) == coords]
    require(len(reverse) == int(not main['oneway']), f'{main["edge_uid"]}: ambiguous or missing reverse direction')
    directions = [(main, position.measure_m)]
    for edge in reverse:
        require(edge['edge_uid'] in eligible and not edge['oneway'] and edge['reversed'] != main['reversed'],
                f'{main["edge_uid"]}: reverse lineage or eligibility differs')
        reverse_measure = edge['geometry'].length - position.measure_m
        require(edge['geometry'].interpolate(reverse_measure).distance(position.point) <= TOL,
                f'{main["edge_uid"]}: reverse measure is not the same physical location')
        directions.append((edge, reverse_measure))
    return directions


def point_record(key, base, row, old, selected, facility, directions):
    method = ('automatic_projection' if row is None else 'manual_entrance_refinement'
              if row['review_decision'] == 'revise' else 'manual_keep')
    return dict(
        county=key[0], facility_id=key[1], facility_name=base['facility_name'], address=base['address'],
        access_point_id=f'access:{key[0]}:{key[1]}',
        selection_method=method, review_decision=row['review_decision'] if row else '',
        review_reason=row['review_reason'] if row else '', review_notes=row['review_notes'] if row else '',
        review_group=row['review_group'] if row else '', reviewed_at_utc=row['reviewed_at_utc'] if row else '',
        clicked_latitude=row['clicked_latitude'] if row else '',
        clicked_longitude=row['clicked_longitude'] if row else '',
        vehicle_entrance_status='unverified',
        facility_latitude=base['facility_latitude'], facility_longitude=base['facility_longitude'],
        facility_x_5186=facility.x, facility_y_5186=facility.y,
        original_projection_latitude=old.latitude, original_projection_longitude=old.longitude,
        original_s0_edge_uid=old.edge['edge_uid'], original_s0_edge_measure_m=old.measure_m,
        original_connector_length_m=facility.distance(old.point),
        final_projection_latitude=selected.latitude, final_projection_longitude=selected.longitude,
        final_projection_x_5186=selected.point.x, final_projection_y_5186=selected.point.y,
        final_connector_length_m=facility.distance(selected.point),
        position_shift_m=selected.point.distance(old.point),
        s0_edge_uid=selected.edge['edge_uid'], s0_edge_measure_m=selected.measure_m,
        osm_way_id=selected.edge['osmid'], highway=selected.edge['highway'],
        s0_direction_count=len(directions),
    )


def scenario_rows(point_row, directions, traces):
    scenarios, links = [], []
    previous = None
    for scenario in SCENARIOS:
        approaches = []
        for parent, measure in directions:
            uid = parent['edge_uid']
            parts = [Span.from_row(parent)] if scenario == 'S0' else traces[scenario][uid]
            link = arrival_at_measure(parts, measure)
            approaches.append(dict(
                access_point_id=point_row['access_point_id'], scenario=scenario,
                county=point_row['county'], facility_id=point_row['facility_id'],
                s0_edge_uid=uid, s0_edge_measure_m=measure, direction_u=parent['u'],
                direction_v=parent['v'], **link,
                arrival_sink_id=f'demand:{point_row["county"]}:{point_row["facility_id"]}',
            ))
        states = {r['point_state'] for r in approaches}
        require(len(states) == 1, f'{point_row["access_point_id"]}: inconsistent physical direction states')
        state = states.pop()
        require(previous is None or STATE_RANK[previous] <= STATE_RANK[state],
                f'{point_row["access_point_id"]}: physical point reopened')
        previous = state
        scenarios.append(dict(
            access_point_id=point_row['access_point_id'], county=point_row['county'],
            facility_id=point_row['facility_id'], facility_name=point_row['facility_name'],
            scenario=scenario, final_projection_latitude=point_row['final_projection_latitude'],
            final_projection_longitude=point_row['final_projection_longitude'],
            point_state=state, touches_internal_cut=any(r['touches_internal_cut'] for r in approaches),
            arrival_count_review_closed=sum(r['arrival_allowed_review_closed'] for r in approaches),
            arrival_count_review_open=sum(r['arrival_allowed_review_open'] for r in approaches),
        ))
        links.extend(approaches)
    return scenarios, links


def flood_rows(point_row, facility, access, masks):
    result = []
    previous = 0.0
    for scenario in WET_SCENARIOS:
        tree = masks[scenario]
        line_state, length = connector_in_mask(facility, access, tree)
        require(length + TOL >= previous,
                f'{point_row["access_point_id"]}: S1/S2 connector overlap is not nested')
        previous = length
        result.append(dict(
            access_point_id=point_row['access_point_id'], county=point_row['county'],
            facility_id=point_row['facility_id'], facility_name=point_row['facility_name'],
            scenario=scenario, facility_point_mask_state=point_in_mask(facility, tree),
            access_point_mask_state=point_in_mask(access, tree), straight_connector_mask_state=line_state,
            straight_connector_length_m=facility.distance(access), straight_connector_interior_overlap_m=length,
        ))
    return result


def resolve_inputs(args):
    traces = {s: args.run / f'{PROFILE}_{s.lower()}_edge_trace.csv' for s in WET_SCENARIOS}
    inputs = dict(s0=args.s0_gpkg or args.run / 'road_scenarios.gpkg',
                  validation=args.run / 'validation.json', flood=args.flood_gpkg,
                  node_tags=args.node_tags, facilities=args.facilities, manual=args.manual_review,
                  projection_summary=args.projection_dir / 'facility_projection_summary.json',
                  automatic=args.projection_dir / 'facility_projection_distances.csv',
                  review_queue=args.projection_dir / 'facility_projection_review_queue.csv',
                  audit_sample=args.projection_dir / 'facility_projection_audit_sample.csv', **traces)
    hashes = {key: file_sha256(path) for key, path in inputs.items()}
    manifest = json.loads(inputs['validation'].read_bytes())
    summary = json.loads(inputs['projection_summary'].read_bytes())
    expected = manifest['output_sha256']
    require(manifest['status'] == 'passed' and manifest['analysis_crs'] == f'EPSG:{CRS}',
            'Expected a passed archived scenario run')
    require(hashes['facilities'] == manifest['facility_sha256'] == summary['inputs']['shelters_final_147.csv']
            and hashes['node_tags'] == summary['inputs']['source_node_tags.json']
            and hashes['flood'] == manifest['flood']['sha256']
            and all(hashes[s] == expected[path.name] for s, path in traces.items()),
            'Archived input hashes differ')
    expected_s0 = (summary['inputs']['s0_road_network.gpkg'] if args.s0_gpkg
                   else expected['road_scenarios.gpkg'])
    require(hashes['s0'] == expected_s0, 'S0 source differs from the archived run or original S0 export')
    return inputs, hashes, manifest


def run(args):
    output = args.output.resolve()
    require(not output.exists() and all(
        not output.is_relative_to(path.resolve()) for path in (args.run, args.projection_dir)
    ), 'Use a new output folder outside the original run and projections')
    inputs, hashes, manifest = resolve_inputs(args)
    original = source_facilities(inputs['facilities'])
    automatic = by_facility(csv_records(inputs['automatic'], (
        'county', 'facility_id', *IDENTITY, 'facility_latitude', 'facility_longitude', *AUTO_FIELDS,
    )))
    require(automatic.keys() == original.keys(), 'Automatic projections must cover the source facilities')
    cohort = read_cohort(args.projection_dir, automatic)
    reviews = read_reviews(inputs['manual'], automatic, cohort)

    nodes = unique(read_saved(inputs['s0'], S0_LAYER, 'nodes'), 'node_id')
    edges = unique([normalize(r) for r in read_saved(inputs['s0'], S0_LAYER, 'edges')], 'edge_uid')
    require((len(nodes), len(edges)) == PROFILES[PROFILE], 'Unexpected S0 node or edge count')
    s0_edges_match(nodes, edges)
    tags = {int(k): v for k, v in json.loads(inputs['node_tags'].read_bytes()).items()}
    eligible = attachment_eligible(nodes, edges, tags)
    checks = ProjectionCheck(edges, eligible)
    by_way = defaultdict(list)
    for r in edges.values():
        by_way[r['osmid']].append(r)

    traces, trace_counts = {}, {}
    for scenario in WET_SCENARIOS:
        traces[scenario], trace_counts[scenario] = read_trace(inputs[scenario], edges, nodes.keys())
        stats = next(r for r in manifest['scenarios'] if r['profile'] == PROFILE and r['scenario'] == scenario)
        counts = trace_counts[scenario]
        require(counts.get('open', 0) + counts.get('review', 0) == stats['active_directed_edges']
                and counts.get('blocked', 0) == stats['blocked_directed_child_edges'],
                f'{scenario}: trace counts differ from archived manifest')
    check_trace_nesting(traces['S1'], traces['S2'])
    masks = read_masks(inputs['flood'])

    points, statuses, links, flooding = [], [], [], []
    for key, base in sorted(automatic.items()):
        reference = original[key]
        facility = coord(reference, 'facility')
        require(all(base[k] == reference[k] for k in IDENTITY)
                and facility.distance(coord(base, 'facility')) <= CSV_TOL_M,
                f'{key}: automatic facility coordinates or identity differ from source')
        old = checks.validate(base, facility, key)
        row = reviews.get(key)
        selected = checks.validate(row, facility, key, revised=True) if row and row['review_decision'] == 'revise' else old
        directions = directed_positions(selected, by_way, eligible)
        point = point_record(key, reference, row, old, selected, facility, directions)
        points.append(point)
        site_status, approach_links = scenario_rows(point, directions, traces)
        statuses.extend(site_status)
        links.extend(approach_links)
        flooding.extend(flood_rows(point, facility, selected.point, masks))

    require(len(points) == FACILITIES and len(statuses) == FACILITIES * len(SCENARIOS)
            and len(flooding) == FACILITIES * len(WET_SCENARIOS), 'Incomplete access outputs')
    summary = dict(
        status='passed', profile=PROFILE, analysis_crs=f'EPSG:{CRS}', facilities=len(points),
        manual_review_decisions=dict(DECISIONS), manual_review_groups=dict(GROUPS),
        manual_review_physical_access_failures_reported=0,
        selection_methods=dict(Counter(r['selection_method'] for r in points)),
        s0_direction_counts=dict(Counter(r['s0_direction_count'] for r in points)),
        trace_direction_child_counts=trace_counts,
        access_point_states={
            s: dict(Counter(r['point_state'] for r in statuses if r['scenario'] == s)) for s in SCENARIOS
        },
        wet_mask_states={
            s: {field: dict(Counter(r[field] for r in flooding if r['scenario'] == s))
                for field in ('facility_point_mask_state', 'access_point_mask_state', 'straight_connector_mask_state')}
            for s in WET_SCENARIOS
        },
        modeled_arrival_link_counts={
            s: dict(review_closed=sum(r['arrival_allowed_review_closed'] for r in links if r['scenario'] == s),
                    review_open=sum(r['arrival_allowed_review_open'] for r in links if r['scenario'] == s))
            for s in SCENARIOS
        },
        refined_facilities=[{field: r[field] for field in (
            'county', 'facility_id', 'facility_name', 'original_connector_length_m', 'final_connector_length_m',
            'position_shift_m', 'original_s0_edge_uid', 's0_edge_uid')}
            for r in points if r['selection_method'] == 'manual_entrance_refinement'],
        csv_coordinate_tolerance_m=CSV_TOL_M, trace_boundary_tolerance_m=TOL,
        s0_provenance='original_review_s0_export' if args.s0_gpkg else 'archived_full_scenario_gpkg',
        input_sha256={key: dict(name=path.name, sha256=hashes[key]) for key, path in inputs.items()},
        source_layers={s: f'scenario_{s.lower()}_closure_zone' for s in WET_SCENARIOS},
        arrival_semantics='Directed approach to a demand-only terminal; lengths in EPSG:5186, no cost assigned.',
        spatial_semantics='Planar representative point and straight connector, distinct from road trace and vehicle access.',
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.facility-access-', dir=output.parent) as scratch:
        stage = Path(scratch) / 'results'
        stage.mkdir()
        write_csv(stage / 'facility_final_access_points.csv', points)
        write_csv(stage / 'access_point_scenarios.csv', statuses)
        write_csv(stage / 'access_point_od_connectors.csv', links)
        write_csv(stage / 'facility_connector_flood.csv', flooding)
        summary['output_sha256'] = {p.name: file_sha256(p) for p in sorted(stage.iterdir())}
        write_json(stage / 'facility_access_qa.json', summary)
        require(all(file_sha256(path) == hashes[key] for key, path in inputs.items()), 'Input changed during analysis')
        require(not output.exists(), f'Output already exists: {output}')
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'flood-gpkg', 'node-tags', 'facilities', 'manual-review', 'projection-dir', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--s0-gpkg', type=Path, help='The same S0-only export used for the projection review')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
