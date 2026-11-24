"""Bind a team-share build to the archived analyses and verified facility rows."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import math
from pathlib import Path

from scripts.facility_access_scenarios import PROFILE, STATE_RANK, WET_SCENARIOS
from scripts.finalize_facility_access_points import csv_records, source_facilities
from scripts.flood_road_inputs import CRS, PROFILES, file_sha256, read_road_metadata, require
from scripts.qa_flood_road_scenarios import SCENARIOS, as_bool

FINAL_FILES = (
    'facility_final_access_points.csv', 'access_point_scenarios.csv',
    'facility_connector_flood.csv', 'access_point_od_connectors.csv',
)
PROJECTION_FILES = {
    'projection_summary': 'facility_projection_summary.json',
    'automatic': 'facility_projection_distances.csv',
    'review_queue': 'facility_projection_review_queue.csv',
    'audit_sample': 'facility_projection_audit_sample.csv',
}
ROAD_METRICS = ('active_directed_edges', 'blocked_directed_child_edges',
                'physical_blocked_m', 'largest_scc_original_nodes')
FLOOD_STATES = ('facility_point_mask_state', 'access_point_mask_state', 'straight_connector_mask_state')
SELECTIONS = {'automatic_projection', 'manual_keep', 'manual_entrance_refinement'}


@dataclass
class InputHashes:
    paths: dict = field(default_factory=dict)

    def record(self, path, expected=None):
        path = path.resolve(strict=True)
        if path not in self.paths:
            self.paths[path] = file_sha256(path)
        digest = self.paths[path]
        require(expected is None or digest == expected, f'SHA-256 mismatch: {path.name}')
        return digest

    def unchanged(self):
        for path, digest in self.paths.items():
            require(file_sha256(path) == digest, f'Input changed during packaging: {path.name}')


def read_json(path):
    return json.loads(path.read_bytes())


def check_input_files(paths):
    missing = [str(path.resolve()) for path in paths if not path.is_file()]
    require(not missing, 'Missing required input files:\n' + '\n'.join(missing))


def index_rows(rows, *fields):
    result = {tuple(r[k] for k in fields): r for r in rows}
    require(len(result) == len(rows), f'Duplicate CSV key: {fields}')
    return result


def read_final(path, hashes, expected_inputs, projection_export_hash):
    qa_path = path / 'facility_access_qa.json'
    check_input_files([qa_path, *(path / name for name in FINAL_FILES)])
    hashes.record(qa_path)
    qa = read_json(qa_path)
    require(qa['status'] == 'passed' and qa['profile'] == PROFILE and qa['analysis_crs'] == f'EPSG:{CRS}',
            'Expected a passed facility run for the map profile')
    recorded = qa['input_sha256']
    require(all(recorded[key]['sha256'] == digest for key, digest in expected_inputs.items() if key != 's0'),
            'Facility run does not match the selected inputs')
    provenance = {
        'archived_full_scenario_gpkg': expected_inputs['s0'],
        'original_review_s0_export': projection_export_hash,
    }.get(qa['s0_provenance'])
    require(provenance and recorded['s0']['sha256'] == provenance, 'Facility run S0 provenance mismatch')
    for name in FINAL_FILES:
        hashes.record(path / name, qa['output_sha256'][name])
    return qa


def select_final(args, hashes, expected_inputs, projection_export_hash, verify_facilities):
    candidates = [args.final_run] if args.final_run else sorted(
        (p for p in args.run.parent.glob('final-facility-access-*') if p.is_dir()), reverse=True)
    rejected = []
    for path in candidates:
        try:
            candidate_hashes = InputHashes()
            qa = read_final(path, candidate_hashes, expected_inputs, projection_export_hash)
            # A matching manifest also has to describe a complete set of validated CSV rows.
            sites, totals = verify_facilities(path, qa)
            hashes.paths.update(candidate_hashes.paths)
            return path, qa, sites, totals, rejected
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rejected.append(f'{path.name}: {exc}')
    raise ValueError(f'No matching passed final-facility-access run in {args.run.parent.resolve()}. '
                     + ' | '.join(rejected))


def validate_roads(path, manifest, independent):
    rows = index_rows(csv_records(path, ('profile', 'scenario', *ROAD_METRICS)), 'profile', 'scenario')
    expected = index_rows(manifest['scenarios'], 'profile', 'scenario')
    require(rows.keys() == expected.keys() == {(p, s) for p in PROFILES for s in SCENARIOS},
            'Incomplete scenario comparison')
    profile_stats = {}
    for key, row in rows.items():
        for field in ROAD_METRICS:
            require(float(row[field]) == float(expected[key][field]), f'{key}: archived {field} differs')
        s = key[1]
        if key[0] == PROFILE:
            stats = independent['profiles'][PROFILE]['scenarios'][s]
            for field in ('active_directed_edges', 'physical_blocked_m', 'largest_scc_original_nodes'):
                tol = 0.1 if field == 'physical_blocked_m' else 0
                require(math.isclose(float(row[field]), float(stats[field]), abs_tol=tol, rel_tol=0),
                        f'{s}: {field} differs from independent QA')
            profile_stats[s] = {k: float(row[k]) if k == 'physical_blocked_m' else int(row[k]) for k in ROAD_METRICS}
            profile_stats[s]['flood_km2'] = (0 if s == 'S0' else float(manifest['flood']['areas_km2'][s]))
    return profile_stats


def validate_facilities(folder, qa, source):
    points, scenarios, floods, links = [folder / n for n in FINAL_FILES]
    keys = ('access_point_id', 'scenario')
    rows = index_rows(csv_records(points, (
        'access_point_id', 'county', 'facility_id', 'facility_name', 'address', 'facility_latitude',
        'facility_longitude', 'final_projection_latitude', 'final_projection_longitude',
        'final_projection_x_5186', 'final_projection_y_5186', 's0_edge_uid', 's0_edge_measure_m',
        'final_connector_length_m', 'selection_method', 'review_decision', 'review_reason',
        'review_notes', 'highway', 's0_direction_count', 'vehicle_entrance_status',
    )), 'county', 'facility_id')
    require(rows.keys() == source.keys() and len(rows) == qa['facilities'], 'Unexpected facility coverage')
    ids = {r['access_point_id'] for r in rows.values()}
    require(len(ids) == len(rows) and all(ids), 'Duplicate or blank facility access ID')
    states = index_rows(csv_records(scenarios, (
        *keys, 'county', 'facility_id', 'facility_name', 'point_state', 'final_projection_latitude',
        'final_projection_longitude', 'arrival_count_review_closed', 'arrival_count_review_open',
    )), *keys)
    wet = index_rows(csv_records(floods, (
        *keys, 'county', 'facility_id', 'facility_name', *FLOOD_STATES, 'straight_connector_interior_overlap_m',
    )), *keys)
    approaches = index_rows(csv_records(links, (
        *keys, 'county', 'facility_id', 's0_edge_uid', 'point_state', 'arrival_node_id', 'arrival_length_m',
        'arrival_allowed_review_closed', 'arrival_allowed_review_open',
    )), *keys, 's0_edge_uid')
    require(states.keys() == {(i, s) for i in ids for s in SCENARIOS}
            and wet.keys() == {(i, s) for i in ids for s in WET_SCENARIOS}, 'Missing scenario facility rows')
    grouped = defaultdict(list)
    for (i, s, _), r in approaches.items():
        require((i, s) in states, 'Unknown arrival link facility or scenario')
        grouped[i, s].append(r)

    sites = []
    crossings = Counter()
    counts = {s: Counter() for s in SCENARIOS}
    arrival = {s: Counter(review_closed=0, review_open=0) for s in SCENARIOS}
    flood_counts = {s: {field: Counter() for field in FLOOD_STATES} for s in WET_SCENARIOS}
    for key, row in sorted(rows.items()):
        original = source[key]
        require(all(row[k] == original[k] for k in ('facility_name', 'address'))
                and all(float(row[k]) == float(original[k]) for k in ('facility_latitude', 'facility_longitude'))
                and row['vehicle_entrance_status'] == 'unverified'
                and row['selection_method'] in SELECTIONS, f'{key}: facility source mismatch')
        site = dict(row, scenarios={})
        i = row['access_point_id']
        previous = -1
        for s in SCENARIOS:
            record = states[i, s]
            require(all(record[k] == row[k] for k in ('county', 'facility_id', 'facility_name',
                        'final_projection_latitude', 'final_projection_longitude')), f'{i}/{s}: fixed position changed')
            status = record['point_state']
            require(status in STATE_RANK and previous <= STATE_RANK[status], f'{i}/{s}: invalid road state')
            previous = STATE_RANK[status]
            counts[s][status] += 1
            directions = grouped[i, s]
            expected_directions = int(row['s0_direction_count'])
            require(expected_directions in (1, 2) and len(directions) == expected_directions,
                    f'{i}/{s}: missing direction records')
            for link in directions:
                closed, opened = (as_bool(link[f'arrival_allowed_review_{p}']) for p in ('closed', 'open'))
                require((link['county'], link['facility_id']) == key and link['point_state'] == status
                        and closed == (status == 'open') and opened == (status != 'blocked')
                        and bool(link['arrival_node_id']) == opened
                        and bool(link['arrival_length_m']) == opened, f'{i}/{s}: invalid arrival link')
                if opened:
                    require(math.isfinite(float(link['arrival_length_m']))
                            and float(link['arrival_length_m']) >= 0, f'{i}/{s}: invalid length')
            totals = {}
            for policy in ('closed', 'open'):
                field = 'arrival_count_review_' + policy
                count = sum(as_bool(d['arrival_allowed_review_' + policy]) for d in directions)
                require(count == int(record[field]), f'{i}/{s}: {field} differs')
                arrival[s]['review_' + policy] += count
                totals[field] = count
            detail = dict(point_state=status, **totals, facility_point_mask_state='outside',
                          straight_connector_interior_overlap_m=0)
            if s in WET_SCENARIOS:
                flood = wet[i, s]
                require(all(flood[k] == row[k] for k in ('county', 'facility_id', 'facility_name')),
                        f'{i}/{s}: flood record identity changed')
                for field in FLOOD_STATES:
                    flood_counts[s][field][flood[field]] += 1
                overlap = float(flood['straight_connector_interior_overlap_m'])
                line_state = flood['straight_connector_mask_state']
                require(flood['facility_point_mask_state'] in ('inside', 'outside', 'boundary')
                        and flood['access_point_mask_state'] in ('inside', 'outside', 'boundary')
                        and line_state in ('partially_overlapped', 'fully_overlapped', 'outside',
                                           'boundary_contact', 'zero_length')
                        and 0 <= overlap <= float(row['final_connector_length_m']) + 1e-6
                        and (overlap > 0) == (line_state in ('partially_overlapped', 'fully_overlapped')),
                        f'{i}/{s}: invalid saved flood record')
                crossings[s] += int(overlap > 0)
                detail.update(facility_point_mask_state=flood['facility_point_mask_state'],
                              straight_connector_interior_overlap_m=overlap)
            site['scenarios'][s] = detail
        sites.append(site)
    selections = Counter(r['selection_method'] for r in rows.values())
    require(qa['selection_methods'] == selections and qa['access_point_states'] == counts
            and qa['wet_mask_states'] == flood_counts and qa['modeled_arrival_link_counts'] == arrival
            and counts['S0'] == {'open': len(sites)}, 'Facility CSV totals differ from passed QA')
    return sites, {
        s: dict(facilities=len(sites), open=counts[s]['open'], blocked=counts[s]['blocked'],
                review=counts[s]['review'], facility_inside=flood_counts.get(s, {})
                .get('facility_point_mask_state', {}).get('inside', 0),
                connector_crossings=crossings[s])
        for s in SCENARIOS
    }


def load_verified_inputs(args):
    hashes = InputHashes()
    road = args.road_artifact / 'road_network_profiles.gpkg'
    road_meta = args.road_artifact / 'metadata.json'
    tags = args.road_artifact / 'source_node_tags.json'
    validation = args.run / 'validation.json'
    paths = dict(s0=args.run / 'road_scenarios.gpkg', validation=validation, flood=args.flood_gpkg,
                 facilities=args.facilities, road=road, road_meta=road_meta, node_tags=tags,
                 road_qa=args.independent_qa, comparison=args.run / 'scenario_comparison.csv',
                 **{s: args.run / f'{PROFILE}_{s.lower()}_edge_trace.csv' for s in WET_SCENARIOS},
                 **{k: args.projection_dir / n for k, n in PROJECTION_FILES.items()})
    if args.manual_review:
        paths['manual'] = args.manual_review
    check_input_files(paths.values())
    digests = {k: hashes.record(path) for k, path in paths.items()}
    manifest = read_json(validation)
    qa = read_json(args.independent_qa)
    projection = read_json(paths['projection_summary'])
    require(manifest['status'] == qa['status'] == 'passed' and manifest['analysis_crs'] == f'EPSG:{CRS}'
            and qa['run_gpkg_sha256'] == digests['s0'] and qa['facility_sha256'] == digests['facilities'],
            'Independent QA does not describe this run')
    require(all(digests[s] == manifest['output_sha256'][paths[s].name]
                for s in ('s0', 'S1', 'S2', 'comparison'))
            and digests['facilities'] == manifest['facility_sha256']
            and digests['flood'] == manifest['flood']['sha256'], 'Scenario run checksums differ')
    _, metadata = read_road_metadata(road, road_meta)
    require(digests['road'] == manifest['roads']['sha256'] == metadata['sha256']
            and digests['road_meta'] == manifest['roads']['metadata_sha256'] == metadata['metadata_sha256']
            and digests['facilities'] == metadata['verified_facility_sha256']
            and digests['node_tags'] == read_json(road_meta)['output_sha256']['source_node_tags.json'],
            'Road profile artifact differs from the saved scenario inputs')
    require(projection['inputs']['shelters_final_147.csv'] == digests['facilities']
            and projection['inputs']['source_node_tags.json'] == digests['node_tags'],
            'Prior projection inputs differ')
    input_keys = {'s0', 'validation', 'flood', 'facilities', 'node_tags', 'S1', 'S2', *PROJECTION_FILES}
    if 'manual' in paths:
        input_keys.add('manual')
    source = source_facilities(paths['facilities'])
    final, final_qa, sites, totals, rejected = select_final(
        args, hashes, {k: digests[k] for k in input_keys}, projection['inputs']['s0_road_network.gpkg'],
        lambda folder, record: validate_facilities(folder, record, source))
    require(projection['facilities'] == len(sites), 'Prior projection count differs')
    road_stats = validate_roads(paths['comparison'], manifest, qa)
    return dict(paths=paths, hashes=hashes, digests=digests, manifest=manifest, qa=qa,
                projection=projection, final=final, final_qa=final_qa, sites=sites, rejected=rejected,
                stats={s: dict(**road_stats[s], **totals[s]) for s in SCENARIOS})
