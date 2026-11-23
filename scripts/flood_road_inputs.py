"""Read-only validation of the archived flood masks and road profile package."""
import json
import logging
import sqlite3
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import geopandas as gpd
import networkx as nx
import shapely
from shapely.geometry import LineString

from scripts.build_flood_depth_scenarios_100y import ABS_AREA_TOL_M2, REL_AREA_TOL, file_sha256

LOG = logging.getLogger(__name__)
CRS = 5186
CODES = tuple(f'N{i}' for i in range(330, 335))
PROFILES = {
    'drive_reference': (14390, 32085),
    'drive_service': (24457, 54893),
    'relief_vehicle_candidate': (24447, 54867),
}
FACILITIES = Path('data/processed/shelters_final_147.csv')
SNAPSHOT = '492ceebb946500fa54dd1b460b4ff923884dd9e673f65d3750e1d60122b85a8e'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def layer(path, name, epsg, types, *, allow_zero_lines=False):
    frame = gpd.read_file(path, layer=name, engine='pyogrio')
    require(frame.crs is not None and frame.crs.to_epsg() == epsg, f'{name}: expected EPSG:{epsg}')
    geom = frame.geometry
    valid = geom.is_valid | (shapely.length(geom.array) == 0) if allow_zero_lines else geom.is_valid
    require(len(frame) and geom.geom_type.isin(types).all() and valid.all()
            and not geom.is_empty.any(), f'{name}: invalid or missing geometry')
    return frame


def read_flood(path, report_path):
    path = Path(path).resolve(strict=True)
    digest = file_sha256(path)
    report_digest = file_sha256(report_path)
    expected = json.loads(Path(report_path).read_bytes())
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'SQLite integrity failure')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'GeoPackage foreign key failure')
    names = dict(zip(('source', 'priority', 'footprint', 'S1', 'S2'), (
        'source_categories', 'depth_priority_5', 'flood_union_footprint',
        'scenario_s1_closure_zone', 'scenario_s2_closure_zone')))
    listing = gpd.list_layers(path).to_dict('records')
    frames = {}
    for key, name in names.items():
        LOG.info('Validating flood layer %s', name)
        frames[key] = layer(path, name, CRS, ('Polygon', 'MultiPolygon'))
    source, priority = frames['source'], frames['priority']
    counts = source.groupby('source_stem')['SEG_CODE'].agg(list).to_dict()
    require(expected['source_crs'] == f'EPSG:{CRS}', 'Reported CRS mismatch')
    require(len(source) == expected['source_feature_count'] == 20
            and set(counts) == {r['stem'] for r in expected['source_files']}
            and all(tuple(sorted(c)) == CODES for c in counts.values()), 'Unexpected source features or codes')
    require(len(priority) == 5 and set(priority.SEG_CODE) == set(CODES), 'Unexpected depth classes')
    require(all(len(frames[k]) == 1 for k in ('footprint', 'S1', 'S2')), 'Expected single mask features')
    masks = {k: frames[k].geometry.iloc[0] for k in ('S1', 'S2')}
    footprint = frames['footprint'].geometry.iloc[0]
    areas = {}
    def compare(name, actual, reported):
        areas[name] = actual
        require(abs(actual - reported) <= 0.00000051, f'Area mismatch for {name}: {actual} vs {reported} km²')
    compare('source_sum', float(source.area.sum()) / 1e6, expected['sum_original_feature_areas_km2'])
    for record in expected['source_files']:
        stem = record['stem']
        compare(stem, float(source.loc[source.source_stem == stem].area.sum()) / 1e6,
                record['feature_area_sum_km2'])
    compare('footprint', footprint.area / 1e6, expected['union_footprint_area_km2'])
    for k, mask in masks.items():
        compare(k, mask.area / 1e6, expected['scenario_masks'][k]['closure_zone_area_km2'])
    by_code = dict(zip(priority.SEG_CODE, priority.geometry))
    for item in expected['depth_classes']:
        code = item['code']
        compare(code, by_code[code].area / 1e6, item['exclusive_priority_area_km2'])
    tolerance = max(ABS_AREA_TOL_M2, REL_AREA_TOL * footprint.area)
    LOG.info('Checking subset, overlap and coverage of flood masks')
    differences = {
        'S1_outside_S2': masks['S1'].difference(masks['S2']).area,
        'S2_footprint_symdiff': masks['S2'].symmetric_difference(footprint).area,
        'priority_pairwise_overlap_sum': sum(a.intersection(b).area for a, b in combinations(by_code.values(), 2)),
        'S1_priority_symdiff': masks['S1'].symmetric_difference(shapely.union_all([by_code[c] for c in CODES[1:]])).area,
        'S2_priority_symdiff': masks['S2'].symmetric_difference(shapely.union_all(list(by_code.values()))).area,
    }
    require(all(v <= tolerance for v in differences.values()), f'Flood topology QA failed: {differences}')
    require(digest == file_sha256(path) and report_digest == file_sha256(report_path), 'Flood inputs changed')
    return masks, {
        'status': 'passed', 'sqlite_integrity': 'ok', 'path': str(path), 'sha256': digest,
        'area_reference_sha256': report_digest, 'source_features': len(source), 'areas_km2': areas,
        'differences_m2': differences, 'topology_tolerance_m2': tolerance,
        'layers': [{**entry, 'crs': str(gpd.read_file(path, layer=entry['name'], rows=0).crs)}
                   for entry in listing],
    }


def read_road_metadata(gpkg, path):
    meta = json.loads(path.read_bytes())
    source = meta['source']
    require(meta['status'] == 'data_validated' and source['sha256'] == SNAPSHOT
            and source['data_timestamp'].startswith('2026-11-20'), 'Unexpected road snapshot')
    require(meta['verification']['source_directed_segments_preserved'], 'Missing direction verification')
    require(meta['verification']['graphml_gpkg_roundtrips'], 'Missing GeoPackage verification')
    digest = file_sha256(gpkg)
    require(digest == meta['output_sha256']['road_network_profiles.gpkg'], 'Road GeoPackage differs from artifact manifest')
    return gpkg, {'source': source, 'path': str(gpkg), 'sha256': digest,
                  'metadata_sha256': file_sha256(path), 'native_crs': 'EPSG:4326',
                  'verified_facility_sha256': meta['input_sha256'][FACILITIES.as_posix()]}


def read_profile(path, profile):
    nodes = layer(path, f'{profile}_simplified_nodes', 4326, ('Point',)).to_crs(CRS)
    edges = layer(path, f'{profile}_simplified_edges', 4326, ('LineString',),
                  allow_zero_lines=True).to_crs(CRS)
    require((len(nodes), len(edges)) == PROFILES[profile], f'{profile}: unexpected node or edge counts')
    require(nodes.osmid.is_unique and edges.edge_uid.notna().all() and edges.edge_uid.is_unique
            and not edges.duplicated(['u', 'v', 'key']).any(), f'{profile}: invalid graph IDs')
    graph = nx.MultiDiGraph(crs=f'EPSG:{CRS}', profile=profile)
    for row in nodes.itertuples(index=False):
        graph.add_node(int(row.osmid), x=row.geometry.x, y=row.geometry.y)
    direction_index = defaultdict(list)
    for row in edges.to_dict('records'):
        u, v, key = (int(row.pop(field)) for field in ('u', 'v', 'key'))
        line = row['geometry']
        positions = list(line.coords)
        require(u in graph and v in graph, f'{profile}: absent edge endpoint')
        for node, xy in ((u, positions[0]), (v, positions[-1])):
            require(abs(complex(*xy) - complex(graph.nodes[node]['x'], graph.nodes[node]['y'])) <= 1e-5,
                    f'{profile}: directed geometry mismatch for {row["edge_uid"]}')
        ordinals = json.loads(row['raw_segments'])
        source_nodes = tuple(map(int, row['source_node_ids'].split(',')))
        require(set(ordinals) and len(ordinals) + 1 == len(positions) == len(source_nodes)
                and (source_nodes[0], source_nodes[-1]) == (u, v) and row['parent_edge_id']
                and len(row['source_segment_ids'].split(';')) == len(ordinals)
                and row['oneway'] in (True, False) and row['reversed'] in (True, False),
                f'{profile}: invalid directed OSM trace for {row["edge_uid"]}')
        require(isinstance(row['highway'], str), 'Missing highway classification')
        row['osmid'] = int(row['osmid'])
        for ordinal, first, last in zip(ordinals, source_nodes[:-1], source_nodes[1:], strict=True):
            direction_index[row['osmid'], ordinal].append((first, last, bool(row['oneway'])))
        row['ordinals'] = ordinals
        row['geometry'] = LineString(positions)
        graph.add_edge(u, v, key=key, **row)
    for segment, directions in direction_index.items():
        first, last, one_way = directions[0]
        expected = [(first, last, True)] if one_way else [(first, last, False), (last, first, False)]
        require(directions == expected, f'{profile}: invalid direction pair for OSM segment {segment}')
    return graph
