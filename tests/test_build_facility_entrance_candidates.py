import csv
import json
from argparse import Namespace

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import LineString, Point

from scripts.build_facility_entrance_candidates import RoadIndex, build
from scripts.flood_road_inputs import file_sha256
from scripts.prepare_flood_road_od import read_entrances


def nodes(at):
    return [dict(node_id=n, wcc=1, scc=1, geometry=Point(xy)) for n, xy in at.items()]


def edge(at, u, v, way, tags, *, oneway=False, internal=()):
    ids = [u, *internal, v]
    return dict(edge_uid=f'{way}/{u}/{v}', osmid=way, u=u, v=v, original_u=u, original_v=v,
                state='open', oneway=oneway, highway=tags['highway'], osm_tags=json.dumps(tags),
                source_node_ids=','.join(map(str, ids)), geometry=LineString([at[n] for n in ids]))


def two_way(at, u, v, way, tags, internal=()):
    return [edge(at, u, v, way, tags, internal=internal),
            edge(at, v, u, way, tags, internal=tuple(reversed(internal)))]


def road_fixture(dx=0, dy=0):
    at = {n: (x + dx, y + dy) for n, (x, y) in {
        1: (0, 0), 2: (50, 0), 3: (0, 18), 4: (45, 18), 5: (45, 45),
    }.items()}
    edges = [
        *two_way(at, 1, 2, 101, {'highway': 'residential'}),
        *two_way(at, 2, 5, 102, {'highway': 'residential'}),
        *two_way(at, 5, 4, 103, {'highway': 'residential'}),
        *two_way(at, 4, 3, 104, {'highway': 'service', 'service': 'driveway'}),
    ]
    tags = {3: {'entrance': 'main', 'motor_vehicle': 'yes'}}
    return nodes(at), edges, tags


def test_driveway_entrance_can_outrank_nearest_road():
    index = RoadIndex(*road_fixture())
    rows, review, projection = index.for_facility(Point(4, 2))
    assert projection.equals(Point(4, 0))
    assert review['nearest_road_way_id'] == 101
    assert review['projection_without_s0_node']
    assert review['recommended_node_id'] == rows[0]['candidate_node_id'] == 3
    nearest_node = next(r for r in rows if r['candidate_node_id'] == 1)
    assert rows[0]['candidate_distance_m'] > nearest_node['candidate_distance_m']
    assert review['review_status'] == 'high_confidence_candidate'
    assert index.for_facility(Point(400, 400))[1]['review_status'] == 'unresolved'


def test_oneway_only_entrance_and_controlled_highway():
    at = {1: (0, 0), 2: (0, 30), 3: (30, 30), 4: (-20, 2), 5: (40, 2)}
    rows = [
        edge(at, 2, 1, 1, {'highway': 'service', 'service': 'driveway'}, oneway=True),
        *two_way(at, 2, 3, 2, {'highway': 'residential'}),
        *two_way(at, 4, 5, 3, {'highway': 'motorway'}),
    ]
    index = RoadIndex(nodes(at), rows, {1: {'entrance': 'main', 'motor_vehicle': 'yes'}})
    candidates, review, _ = index.for_facility(Point(1, 2))
    assert review['nearest_road_way_id'] == 3
    assert review['nearest_road_access_screened_out']
    assert review['review_status'] == 'needs_review'
    assert review['recommended_node_id'] in (2, 3)
    entrance = next(r for r in candidates if r['candidate_node_id'] == 1)
    assert entrance['screened_inbound']
    assert not entrance['screened_outbound']
    assert entrance['s0_out_degree'] == 0
    assert 'oneway_incident_road' in entrance['cautions']
    highway_only = RoadIndex(nodes({n: at[n] for n in (4, 5)}), rows[-2:], {})
    _, review, _ = highway_only.for_facility(Point(0, 2))
    assert review['recommended_node_id'] is None
    assert review['review_status'] == 'unresolved'


def test_removed_tagged_node_and_privately_controlled_driveway():
    at = {1: (0, 0), 2: (40, 0), 3: (20, 0), 4: (10, 15)}
    rows = [
        *two_way(at, 1, 2, 1, {'highway': 'residential'}, internal=(3,)),
        *two_way(at, 1, 4, 2, {'highway': 'service', 'service': 'driveway', 'access': 'private'}),
    ]
    index = RoadIndex(nodes({n: at[n] for n in (1, 2, 4)}), rows,
                      {3: {'entrance': 'main', 'motor_vehicle': 'yes'}})
    candidates, review, _ = index.for_facility(Point(20, 1))
    assert review['removed_source_nodes_near_projection'] == '3'
    assert review['removed_tagged_entrances_near_facility'] == '3'
    assert review['projection_without_s0_node']
    assert 'tagged_entrance_source_node_missing_from_s0' in review['review_reasons']
    assert review['recommended_node_id'] in (1, 2)
    assert 3 not in {r['candidate_node_id'] for r in candidates}
    private = next(r for r in candidates if r['candidate_node_id'] == 4)
    assert not private['screened_inbound']
    assert 'explicit_vehicle_or_permission_restriction' in private['cautions']


def test_nearby_competing_driveway_requires_review():
    at = {1: (0, 0), 2: (15, 0), 3: (0, 30)}
    rows = [*two_way(at, 1, 2, 1, {'highway': 'service', 'service': 'driveway'}),
            *two_way(at, 2, 3, 2, {'highway': 'residential'})]
    tags = {1: {'entrance': 'main', 'motor_vehicle': 'yes'}}
    index = RoadIndex(nodes(at), rows, tags)
    _, review, _ = index.for_facility(Point(2, 0))
    assert review['recommended_node_id'] == 1
    assert review['review_status'] == 'needs_review'
    assert 'competing_entrance_evidence' in review['review_reasons']


def test_bridge_is_excluded_as_a_local_entry():
    at = {1: (0, 0), 2: (10, 0)}
    index = RoadIndex(nodes(at), two_way(at, 1, 2, 1, {
        'highway': 'residential', 'layer': '1'
    }), {})
    candidates, review, _ = index.for_facility(Point(0, 1))
    assert all('bridge_or_elevated' in r['cautions'] for r in candidates)
    assert review['recommended_node_id'] is None


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def test_outputs_are_reproducible_and_od_compatible(tmp_path):
    run = tmp_path / 'saved'
    run.mkdir()
    gpkg = run / 'road_scenarios.gpkg'
    facility_path = tmp_path / 'shelters_final_147_verified.csv'
    metadata_path = tmp_path / 'metadata.json'
    node_tags_path = tmp_path / 'source_node_tags.json'
    qa_path = tmp_path / 'qa.json'
    data_nodes, edges, node_tags = road_fixture(200000, 450000)
    for kind, data in [('nodes', data_nodes), ('edges', edges)]:
        gpd.GeoDataFrame(data, crs=5186).to_file(gpkg, layer=f'relief_vehicle_candidate_s0_{kind}')
    node_tags_path.write_text(json.dumps(node_tags), encoding='utf-8')
    metadata_path.write_text(json.dumps(dict(output_sha256={
        node_tags_path.name: file_sha256(node_tags_path),
    })), encoding='utf-8')
    transform = Transformer.from_crs(5186, 4326, always_xy=True)
    lon, lat = transform.transform(200004, 450002)
    with facility_path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['시군명', '시설일련번호', '시설명', 'latitude', 'longitude'])
        writer.writerows(('Test', n, '</script> 확인', lat, lon) for n in range(146))
        writer.writerow(['Test', 146, 'Missing geocode', '', ''])
    manifest = dict(status='passed', output_sha256={gpkg.name: file_sha256(gpkg)},
                    facility_sha256=file_sha256(facility_path),
                    roads=dict(metadata_sha256=file_sha256(metadata_path)))
    (run / 'validation.json').write_text(json.dumps(manifest), encoding='utf-8')
    qa_path.write_text(json.dumps(dict(status='passed', run_gpkg_sha256=file_sha256(gpkg),
        facility_sha256=manifest['facility_sha256'], profiles={'relief_vehicle_candidate': {
            'scenarios': {'S0': dict(active_nodes=len(data_nodes), active_directed_edges=len(edges))}}})),
        encoding='utf-8')
    args = Namespace(run=run, facilities=facility_path, road_metadata=metadata_path,
                     node_tags=node_tags_path, scenario_qa=qa_path, output=tmp_path / 'first')
    build(args)
    first = args.output
    args.output = tmp_path / 'second'
    build(args)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {
        p.name: p.read_bytes() for p in args.output.iterdir()}
    template = first / 'facility_entrances_template.csv'
    assert all(r['entrance_status'] == 'unverified' and r['entrance_node_id'] == '' for r in read_csv(template))
    assert len(read_entrances(template, [({'시군명': 'Test', '시설일련번호': str(n)}, None)
                                        for n in range(146)], set())) == 146
    html = (first / 'facility_entrance_review.html').read_text(encoding='utf-8')
    assert '\\u003c/script> 확인' in html
    report = json.loads((first / 'entrance_candidate_qa.json').read_bytes())
    assert report['review_status_counts']['high_confidence_candidate'] == 146
    assert report['input_sha256']['facilities'] == manifest['facility_sha256']
    with pytest.raises(ValueError, match='new output folder'):
        build(args)
    args.output = tmp_path / 'tampered'
    facility_path.write_bytes(facility_path.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='checksums do not agree'):
        build(args)
    assert not args.output.exists()
