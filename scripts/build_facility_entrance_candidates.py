"""Generate local review material for facility vehicle entrances on audited S0."""
import argparse
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import geopandas as gpd
import networkx as nx
import shapely
from pyproj import Transformer
from shapely.geometry import Point, mapping

from scripts.build_flood_road_scenarios import read_facilities, write_csv, write_json
from scripts.compare_road_profiles import ACCESS_CONTROLLED
from scripts.flood_road_inputs import CRS, file_sha256, require
from scripts.qa_flood_road_scenarios import as_bool, read_saved, review_structure, unique
from scripts.road_vehicle_policy import vehicle_policy

PROFILE = 'relief_vehicle_candidate'
LAYER = PROFILE + '_s0'
DEFAULT_FACILITIES = Path('local_inputs/shelters_final_147_verified.csv')
RADIUS_M = 150
HIGH_DISTANCE_M = 25
AMBIGUITY_M = 15
MAX_CANDIDATES = 5
REMOVED_VERTEX_M = 5
TOPOLOGY_TOL_M = 1e-5
ENTRANCE_TAGS = {'yes', 'main', 'service', 'garage'}
VEHICLE_ALLOWED = {'yes', 'designated'}
TAG_KEYS = {'highway', 'entrance', 'service', 'oneway', 'junction', 'bridge', 'layer',
            'man_made', 'location', 'vehicle', 'motor_vehicle', 'motorcar', 'hgv', 'access',
            'barrier', 'locked', 'surface', 'smoothness', 'width'}


def relevant(tags):
    return {k: v for k, v in tags.items() if k in TAG_KEYS or k.startswith(('max', 'oneway:'))
            or ':conditional' in k}


def json_cell(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def vehicle_entrance(tags):
    return tags.get('entrance') in ENTRANCE_TAGS and any(
        tags.get(k) in VEHICLE_ALLOWED for k in ('vehicle', 'motor_vehicle', 'hgv')
    )


class RoadIndex:
    def __init__(self, nodes, edges, node_tags):
        self.nodes = unique(nodes, 'node_id')
        ids = sorted(self.nodes)
        self.node_ids = ids
        self.node_points = [self.nodes[n]['geometry'] for n in ids]
        self.node_tree = shapely.STRtree(self.node_points)
        self.node_tags = node_tags
        self.removed = {}
        self.arcs = defaultdict(list)
        self.eligible = nx.DiGraph()
        self.eligible.add_nodes_from(ids)
        physical = {}
        for row in sorted(edges, key=lambda r: r['edge_uid']):
            line = row['geometry']
            source_nodes = list(map(int, row['source_node_ids'].split(',')))
            u, v = row['u'], row['v']
            require(row['state'] == 'open' and (u, v) == (row['original_u'], row['original_v'])
                    == (source_nodes[0], source_nodes[-1]) and u in self.nodes and v in self.nodes
                    and len(source_nodes) == len(line.coords), 'Unexpected S0 edge topology')
            tags = json.loads(row['osm_tags'])
            tagged_nodes = [node_tags[n] for n in source_nodes if n in node_tags]
            policy = vehicle_policy(tags, tagged_nodes)
            node_policies = [vehicle_policy(t) for t in tagged_nodes]
            other_warnings = set(policy['review_reasons'])
            for node_policy in node_policies:
                other_warnings.update(node_policy['review_reasons'])
            other_warnings.discard('physical_access_and_relief_vehicle_suitability_unverified')
            controlled = row['highway'] in ACCESS_CONTROLLED
            structure = review_structure(row['osm_tags'])
            blocked = (controlled or structure or not policy['candidate_included']
                       or any(not p['candidate_included'] for p in node_policies)
                       or 'source_node_barrier_or_access_restriction' in other_warnings)
            if controlled:
                other_warnings.add('access_controlled_road')
            if structure:
                other_warnings.add('bridge_or_elevated')
            oneway = as_bool(row['oneway'])
            if oneway:
                other_warnings.add('oneway_incident_road')
            arc = dict(way=int(row['osmid']), edge_uid=row['edge_uid'], u=u, v=v,
                       highway=row['highway'], tags=relevant(tags),
                       warnings=sorted(other_warnings), eligible=not blocked,
                       oneway=oneway, driveway=tags.get('service') == 'driveway')
            for n in {u, v}:
                self.arcs[n].append(arc)
            if not blocked:
                self.eligible.add_edge(u, v)
            for n, xy in zip(source_nodes[1:-1], list(line.coords)[1:-1], strict=True):
                if n not in self.nodes:
                    point = Point(xy)
                    previous = self.removed.setdefault(n, point)
                    require(previous.distance(point) <= TOPOLOGY_TOL_M,
                            f'Conflicting source coordinates for removed node {n}')
            if line.length > 0:
                key = arc['way'], shapely.normalize(line).wkb
                physical.setdefault(key, (line, arc, source_nodes))
        self.road_rows = list(physical.values())
        self.road_tree = shapely.STRtree([line for line, _, _ in self.road_rows])
        self.removed_ids = sorted(n for n in self.removed if node_tags.get(n, {}).get('entrance') in ENTRANCE_TAGS)
        self.removed_tree = shapely.STRtree([self.removed[n] for n in self.removed_ids])
        groups = defaultdict(set)
        for n, row in self.nodes.items():
            groups[row['scc']].add(n)
        self.core = max(groups.values(), key=lambda g: (len(g), -min(g)), default=set())
        # Reachability via the local access-screened graph. This screen is a review heuristic.
        self.screen_core = max(nx.strongly_connected_components(self.eligible.subgraph(self.core)),
                               key=lambda g: (len(g), -min(g)), default=set())
        self.from_core = self.closure(self.eligible)
        self.to_core = self.closure(self.eligible.reverse(copy=False))

    def closure(self, graph):
        seeds = self.screen_core
        visited = set(seeds)
        pending = list(seeds)
        while pending:
            for n in graph.successors(pending.pop()):
                if n not in visited:
                    visited.add(n)
                    pending.append(n)
        return visited

    def nearest_road(self, point):
        indices, distances = self.road_tree.query_nearest(point, return_distance=True)
        if not len(indices):
            return {}, None
        i = min(indices, key=lambda j: (self.road_rows[j][1]['way'], int(j)))
        line, arc, ids = self.road_rows[i]
        measure = line.project(point)
        projection = line.interpolate(measure)
        interior = TOPOLOGY_TOL_M < measure < line.length - TOPOLOGY_TOL_M
        # Only the endpoints of this directed edge are graph junctions. Coincident
        # coordinates on another way do not provide a connection.
        missing = interior
        removed = sorted((self.removed[n].distance(projection), n) for n in set(ids) & self.removed.keys()
                         if self.removed[n].distance(projection) <= REMOVED_VERTEX_M)
        return dict(nearest_road_way_id=arc['way'], nearest_road_edge_uid=arc['edge_uid'],
                    nearest_road_projection_measure_m=measure, nearest_road_highway=arc['highway'],
                    nearest_road_tags=json_cell(arc['tags']), nearest_road_distance_m=float(distances[0]),
                    nearest_road_projection_interior=interior, projection_without_s0_node=missing,
                    removed_source_nodes_near_projection=';'.join(str(n) for _, n in removed),
                    equally_near_osm_ways=len({self.road_rows[j][1]['way'] for j in indices}),
                    nearest_road_access_screened_out=not arc['eligible']), projection

    def candidate(self, node, point):
        row = self.nodes[node]
        arcs = self.arcs[node]
        usable = [d for d in arcs if d['eligible']]
        tags = self.node_tags.get(node, {})
        driveway = any(d['driveway'] for d in usable)
        service = any(d['highway'] == 'service' for d in usable)
        entrance = tags.get('entrance') in ENTRANCE_TAGS
        explicit = vehicle_entrance(tags)
        inbound = any(d['v'] == node for d in usable)
        outbound = any(d['u'] == node for d in usable)
        connected_in, connected_out = node in self.from_core, node in self.to_core
        reasons = []
        if explicit:
            reasons.append('mapped_entrance_with_vehicle_permission_tag')
        elif entrance:
            reasons.append('mapped_entrance_vehicle_use_unspecified')
        if driveway:
            reasons.append('incident_driveway')
        elif service:
            reasons.append('incident_service_road')
        if not reasons:
            reasons.append('nearby_retained_s0_node')
        warnings = {warning for d in arcs for warning in d['warnings']}
        # Node tags also apply if all ways at this node carry permissive tags.
        warnings.update(set(vehicle_policy({}, [tags])['review_reasons']) - {
            'physical_access_and_relief_vehicle_suitability_unverified'
        })
        warnings.update(set(vehicle_policy(tags)['review_reasons']) - {
            'physical_access_and_relief_vehicle_suitability_unverified'
        })
        if not inbound or not outbound:
            warnings.add('missing_screened_inbound_or_outbound')
        if not connected_in or not connected_out:
            warnings.add('missing_screened_round_trip_to_core')
        # Lexicographic review ordering. No score or probability is estimated.
        tier = 0 if explicit and driveway else 1 if explicit else 2 if driveway else 3 if (
            entrance or service) else 4
        return dict(candidate_node_id=node, candidate_distance_m=point.distance(row['geometry']),
                    highway_types=';'.join(sorted({d['highway'] for d in arcs})),
                    incident_osm_way_ids=';'.join(map(str, sorted({d['way'] for d in arcs}))),
                    incident_osm_tags=json_cell(arcs), node_osm_tags=json_cell(relevant(tags)),
                    s0_wcc=row['wcc'], s0_scc=row['scc'], s0_in_degree=sum(d['v'] == node for d in arcs),
                    s0_out_degree=sum(d['u'] == node for d in arcs), screened_inbound=inbound,
                    screened_outbound=outbound, reachable_from_screened_core=connected_in,
                    can_return_to_screened_core=connected_out, selection_basis=';'.join(reasons),
                    cautions=';'.join(sorted(warnings))), tier, bool(usable)

    def for_facility(self, point):
        road, projection = self.nearest_road(point)
        found = [self.candidate(self.node_ids[i], point)
                 for i in self.node_tree.query(point, predicate='dwithin', distance=RADIUS_M)]
        found.sort(key=lambda entry: (not entry[2],
                                      not (entry[0]['reachable_from_screened_core']
                                           and entry[0]['can_return_to_screened_core']), entry[1],
                                      entry[0]['candidate_distance_m'], entry[0]['candidate_node_id']))
        usable = [entry for entry in found if entry[2]]
        removed_entrances = sorted(self.removed_ids[i] for i in self.removed_tree.query(
            point, predicate='dwithin', distance=RADIUS_M))
        reasons = []
        status = 'unresolved'
        if not usable:
            reasons.append('no_screened_s0_candidate_within_search_radius')
        else:
            best, tier, _ = usable[0]
            status = 'needs_review'
            if tier != 0:
                reasons.append('no_combined_mapped_vehicle_entrance_and_driveway_evidence')
            if best['candidate_distance_m'] > HIGH_DISTANCE_M:
                reasons.append('recommended_node_beyond_close_evidence_distance')
            if best['cautions']:
                reasons.append('recommended_node_has_access_direction_or_connectivity_cautions')
            if any(other_tier <= max(tier, 2)
                   and other['candidate_distance_m'] <= best['candidate_distance_m'] + AMBIGUITY_M
                   for other, other_tier, _ in usable[1:]):
                reasons.append('competing_entrance_evidence')
        if removed_entrances:
            reasons.append('tagged_entrance_source_node_missing_from_s0')
        if road.get('nearest_road_access_screened_out'):
            reasons.append('nearest_road_screened_out')
        if road.get('projection_without_s0_node'):
            # Generic interior proximity is common. It becomes competing evidence when
            # the closest road is a driveway or there is a nearby omitted source vertex.
            if road.get('removed_source_nodes_near_projection') or (
                    json.loads(road['nearest_road_tags']).get('service') == 'driveway'):
                reasons.append('check_interior_road_projection_without_s0_node')
        if road.get('equally_near_osm_ways', 0) > 1:
            reasons.append('multiple_equally_near_osm_ways')
        if usable and not reasons:
            status = 'high_confidence_candidate'
        candidates = [row for row, _, _ in found[:MAX_CANDIDATES]]
        recommended = usable[0][0]['candidate_node_id'] if usable else None
        review = dict(**road, review_status=status, recommended_node_id=recommended,
                      alternate_node_ids=';'.join(str(row['candidate_node_id']) for row in candidates
                                                  if row['candidate_node_id'] != recommended),
                      additional_review_needed=status != 'high_confidence_candidate',
                      review_reasons=';'.join(reasons), nearby_s0_node_count=len(found),
                      screened_candidate_count=len(usable),
                      removed_tagged_entrances_near_facility=';'.join(map(str, removed_entrances)))
        return candidates, review, projection


def coordinates(point, to_wgs84, prefix):
    if point is None:
        return {}
    longitude, latitude = to_wgs84.transform(point.x, point.y)
    return {f'{prefix}_longitude': longitude, f'{prefix}_latitude': latitude}


def connectivity_summary(index):
    return dict(wcc_count=len({r['wcc'] for r in index.nodes.values()}),
                scc_count=len({r['scc'] for r in index.nodes.values()}),
                primary_scc_nodes=len(index.core), screened_core_nodes=len(index.screen_core))


def distance_distribution(values):
    ordered = sorted(values)
    if not ordered:
        return dict(count=0, min_m=None, median_m=None, p90_m=None, max_m=None)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    p90 = ordered[(9 * len(ordered) + 9) // 10 - 1]
    return dict(count=len(ordered), min_m=ordered[0], median_m=median, p90_m=p90, max_m=ordered[-1])


def evaluate(index, facilities):
    to_wgs84 = Transformer.from_crs(CRS, 4326, always_xy=True)
    candidate_rows, review_rows, map_sites, road_ids = [], [], [], set()
    for identity, point in sorted(facilities, key=lambda entry: (
            entry[0]['시군명'], entry[0]['시설일련번호'])):
        facility = dict(county=identity['시군명'], facility_id=identity['시설일련번호'],
                        facility_name=identity['시설명'], **coordinates(point, to_wgs84, 'facility'))
        candidates, review, projection = index.for_facility(point)
        review_rows.append(dict(**facility, **review, **coordinates(projection, to_wgs84, 'projection')))
        markers = []
        for rank, row in enumerate(candidates, 1):
            node = row['candidate_node_id']
            location = coordinates(index.nodes[node]['geometry'], to_wgs84, 'candidate')
            candidate_rows.append(dict(**facility, candidate_rank=rank, **row, **location,
                                       review_status=review['review_status'],
                                       recommended=node == review['recommended_node_id']))
            markers.append(dict(node=node, rank=rank, coords=[location['candidate_latitude'],
                                location['candidate_longitude']], distance=row['candidate_distance_m'],
                                basis=row['selection_basis'], cautions=row['cautions'],
                                recommended=node == review['recommended_node_id']))
        removed = []
        for field in ('removed_source_nodes_near_projection', 'removed_tagged_entrances_near_facility'):
            removed.extend(map(int, filter(None, review.get(field, '').split(';'))))
        map_sites.append(dict(**facility, status=review['review_status'], reasons=review['review_reasons'],
                              candidates=markers, projection=coordinates(projection, to_wgs84, 'projection'),
                              projection_missing=review.get('projection_without_s0_node', False),
                              removed=[dict(node=n, **coordinates(index.removed[n], to_wgs84, 'source'))
                                       for n in sorted(set(removed))]))
        road_ids.update(index.road_tree.query(point, predicate='dwithin', distance=RADIUS_M))
    arcs = [index.road_rows[i] for i in sorted(road_ids)]
    local_lines = gpd.GeoSeries([line for line, _, _ in arcs], crs=CRS).to_crs(4326)
    roads = [dict(type='Feature', geometry=mapping(line),
                  properties=dict(highway=arc['highway'], way=arc['way'], screened=not arc['eligible']))
             for line, (_, arc, _) in zip(local_lines, arcs, strict=True)]
    return candidate_rows, review_rows, dict(sites=map_sites, roads=dict(type='FeatureCollection', features=roads))


def write_outputs(stage, inputs, index, facilities):
    candidates, reviews, map_data = evaluate(index, facilities)
    keys = {(r['county'], r['facility_id']) for r in reviews}
    expected = {(r['시군명'], r['시설일련번호']) for r, _ in facilities}
    require(keys == expected and len(keys) == len(reviews) == len(facilities),
            'Missing or duplicate facility in review')
    pairs = {(r['county'], r['facility_id'], r['candidate_node_id']) for r in candidates}
    require(len(pairs) == len(candidates) and all(
        (c, f) in expected and n in index.nodes for c, f, n in pairs), 'Duplicate or non-S0 candidate')
    require(all((r['county'], r['facility_id'], r['recommended_node_id']) in pairs
                for r in reviews if r['recommended_node_id'] is not None), 'Recommended node missing from candidates')
    write_csv(stage / 'facility_entrance_candidates.csv', candidates, (
        'county', 'facility_id', 'facility_name', 'facility_longitude', 'facility_latitude',
        'candidate_node_id', 'candidate_longitude', 'candidate_latitude', 'candidate_rank',
        'candidate_distance_m', 'highway_types', 'incident_osm_way_ids', 'incident_osm_tags', 'node_osm_tags',
        'selection_basis', 'review_status', 'cautions', 'recommended', 's0_wcc', 's0_scc',
        's0_in_degree', 's0_out_degree', 'screened_inbound', 'screened_outbound',
        'reachable_from_screened_core', 'can_return_to_screened_core',
    ))
    write_csv(stage / 'facility_entrance_review.csv', reviews, (
        'county', 'facility_id', 'facility_name', 'recommended_node_id', 'alternate_node_ids',
        'review_status', 'additional_review_needed', 'review_reasons',
    ))
    write_csv(stage / 'facility_entrances_template.csv', [
        dict(county=r['county'], facility_id=r['facility_id'], entrance_node_id='',
             entrance_status='unverified', evidence='') for r in reviews
    ])
    template = Path(__file__).with_name('facility_entrance_review_template.html').read_text(encoding='utf-8')
    # Escape angle brackets so embedded JSON cannot close the script element.
    data = json_cell(map_data).replace('<', r'\u003c')
    (stage / 'facility_entrance_review.html').write_text(template.replace('__REVIEW_DATA__', data), encoding='utf-8')
    status_counts = Counter(r['review_status'] for r in reviews)
    duplicate_checks = dict(missing_facilities=0, duplicate_facilities=0, duplicate_candidates=0,
                            candidates_outside_s0=0)
    write_json(stage / 'entrance_candidate_qa.json', dict(
        status='passed', profile=PROFILE, scenario='S0', input_sha256=inputs, facilities=len(keys),
        candidate_rows=len(candidates), checks=duplicate_checks, connectivity=connectivity_summary(index),
        review_status_counts={s: status_counts[s] for s in (
            'high_confidence_candidate', 'needs_review', 'unresolved')},
        candidate_distance_m=distance_distribution([r['candidate_distance_m'] for r in candidates]),
        recommended_distance_m=distance_distribution([r['candidate_distance_m'] for r in candidates
                                                      if r['recommended']]),
        unresolved_facilities=[{k: r[k] for k in ('county', 'facility_id')}
                               for r in reviews if r['review_status'] == 'unresolved'],
        additional_review=[{k: r[k] for k in ('county', 'facility_id', 'review_status', 'review_reasons')}
                           for r in reviews if r['additional_review_needed']],
        projection_without_s0_node=[{k: r[k] for k in (
            'county', 'facility_id', 'nearest_road_way_id', 'nearest_road_edge_uid',
            'removed_source_nodes_near_projection')}
                                    for r in reviews if r.get('projection_without_s0_node')],
        criteria=dict(
            projected_crs=f'EPSG:{CRS}', search_radius_m=RADIUS_M, near_evidence_distance_m=HIGH_DISTANCE_M,
            competing_evidence_distance_margin_m=AMBIGUITY_M, max_candidates_per_facility=MAX_CANDIDATES,
            removed_vertex_projection_margin_m=REMOVED_VERTEX_M, projection_endpoint_tolerance_m=TOPOLOGY_TOL_M,
            core='largest S0 SCC by node count, then smallest node ID; largest screened SCC within it',
            order=['passes local access screen', 'screened round trip to core', 'tag evidence tier',
                   'planar distance', 'node ID'],
            evidence_tiers=['mapped vehicle entrance with driveway', 'mapped vehicle entrance',
                            'driveway', 'entrance or service', 'other retained S0 node'],
            high_confidence='highest evidence tier, within near distance, no recorded cautions or conflicts',
            screen='no access-controlled highway, structure, explicit vehicle prohibition or mapped barrier',
            unverified='all generated OD template rows; any verification requires human evidence',
        ),
        output_sha256={p.name: file_sha256(p) for p in sorted(stage.iterdir())},
    ))


def build(args):
    gpkg = args.run / 'road_scenarios.gpkg'
    validation_path = args.run / 'validation.json'
    paths = dict(road_scenarios=gpkg, validation=validation_path, independent_qa=args.scenario_qa,
                 facilities=args.facilities)
    if args.node_tags:
        require(args.road_metadata is not None, '--node-tags requires --road-metadata')
        paths.update(source_node_tags=args.node_tags, road_metadata=args.road_metadata)
    else:
        require(args.road_metadata is None, '--road-metadata requires --node-tags')
    output = args.output.resolve()
    require(not output.exists() and not output.is_relative_to(args.run.resolve()),
            'Choose a new output folder outside the saved run')
    require(all(not path.resolve().is_relative_to(output) for path in paths.values()),
            'Output must not contain any input')
    hashes = {name: file_sha256(path) for name, path in paths.items()}
    manifest = json.loads(validation_path.read_bytes())
    qa = json.loads(args.scenario_qa.read_bytes())
    require(manifest['status'] == qa['status'] == 'passed'
            and qa['profiles'][PROFILE]['scenarios']['S0']
            and hashes['road_scenarios'] == manifest['output_sha256'][gpkg.name] == qa['run_gpkg_sha256']
            and hashes['facilities'] == manifest['facility_sha256'] == qa['facility_sha256'],
            'Scenario/QA/verified facility checksums do not agree')
    node_tags = {}
    if args.node_tags:
        meta = json.loads(args.road_metadata.read_bytes())
        require(hashes['road_metadata'] == manifest['roads']['metadata_sha256']
                and hashes['source_node_tags'] == meta['output_sha256']['source_node_tags.json'],
                'Node tags do not trace to the original profile artifact')
        node_tags = {int(k): v for k, v in json.loads(args.node_tags.read_bytes()).items()}
    nodes = read_saved(gpkg, LAYER, 'nodes')
    edges = read_saved(gpkg, LAYER, 'edges')
    expected = qa['profiles'][PROFILE]['scenarios']['S0']
    require(len(nodes) == expected['active_nodes'] and len(edges) == expected['active_directed_edges'],
            'S0 layer counts differ from independent QA')
    index = RoadIndex(nodes, edges, node_tags)
    facilities = read_facilities(args.facilities)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.entrance-candidates-', dir=output.parent) as temp:
        stage = Path(temp) / 'results'
        stage.mkdir()
        write_outputs(stage, hashes, index, facilities)
        require(all(file_sha256(path) == hashes[name] for name, path in paths.items()),
                'An entrance candidate input changed')
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'scenario-qa', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--facilities', type=Path, default=DEFAULT_FACILITIES)
    parser.add_argument('--node-tags', type=Path)
    parser.add_argument('--road-metadata', type=Path)
    build(parser.parse_args())


if __name__ == '__main__':
    main()
