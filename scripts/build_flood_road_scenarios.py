"""Build directional S0/S1/S2 road scenarios from verified local inputs."""
import argparse
import csv
import json
import logging
import math
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import folium
import geopandas as gpd
import networkx as nx
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, mapping
from shapely.ops import substring
from pyogrio import read_info, write_dataframe

from scripts.connected_access_qa import edge_eligibility, reference_core
from scripts.flood_road_inputs import (
    CRS, FACILITIES, PROFILES, file_sha256, read_flood, read_profile, read_road_metadata, require,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ('S0', 'S1', 'S2')
LOG = logging.getLogger(__name__)
M_EPS = 1e-7  # Numerical topology tolerance, in projected metres.


class Mask:
    def __init__(self, geometry):
        self.parts = list(shapely.get_parts(geometry))
        self.tree = shapely.STRtree(self.parts)
        self.boundaries = [p.boundary for p in self.parts]
        # Index boundary segments in long rings to avoid scanning a detailed ring per road.
        self.boundary_chunks = []
        for boundary in self.boundaries:
            for ring in shapely.get_parts(boundary):
                coords = list(ring.coords)
                self.boundary_chunks.extend(LineString(coords[i:i + 257])
                                            for i in range(0, len(coords) - 1, 256))
        self.boundary_tree = shapely.STRtree(self.boundary_chunks)
        shapely.prepare(self.parts)

    def measure(self, line):
        """Return interior intervals in line measure and any boundary-only contact."""
        hits = self.tree.query(line)
        if not any(self.parts[i].intersects(line) for i in hits):
            return [], False
        # A straight segment whose endpoints and extent lie inside has no cut.
        if any(self.parts[i].contains_properly(line) for i in hits):
            return [(0.0, line.length)], False
        cuts = {0.0, line.length}
        boundary_spans = []
        boundary_contact = False
        for i in self.boundary_tree.query(line, predicate='intersects'):
            boundary_contact = True
            hit = line.intersection(self.boundary_chunks[i])
            for part in shapely.get_parts(hit):
                measures = [line.project(Point(xy)) for xy in shapely.get_coordinates(part)]
                cuts.update(measures)
                if part.geom_type == 'LineString':
                    boundary_spans.append((min(measures), max(measures)))
        intervals = []
        ordered = sorted(cuts)
        for start, end in zip(ordered, ordered[1:]):
            if end - start <= M_EPS:
                continue
            measure = (start + end) / 2
            point = line.interpolate(measure)
            overlapping = any(lo <= measure <= hi for lo, hi in boundary_spans)
            if not overlapping and any(self.parts[i].contains(point) for i in hits):
                intervals.append((start, end))
        return intervals, boundary_contact


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + M_EPS:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def structure_review(data):
    tags = json.loads(data['osm_tags'])
    bridge = tags.get('bridge', 'no').lower() not in ('', 'no', 'false', '0')
    try:
        elevated = float(tags.get('layer', 0)) > 0
    except ValueError:
        elevated = True
    elevated |= tags.get('man_made') == 'viaduct' or tags.get('location') == 'elevated'
    return bridge or elevated


def overlay(graph, masks):
    """Measure each physical OSM segment once; accumulate directional measures."""
    cache = {}
    results = {}
    for index, (u, v, key, data) in enumerate(graph.edges(keys=True, data=True), 1):
        line = data['geometry']
        coords = list(line.coords)
        offset = 0.0
        intervals = {s: [] for s in masks}
        touches = {s: False for s in masks}
        for ordinal, a, b in zip(data['ordinals'], coords[:-1], coords[1:], strict=True):
            physical = (data['osmid'], ordinal)
            start, end = sorted((a, b))
            segment = LineString((start, end))
            length = segment.length
            if physical not in cache:
                cache[physical] = (start, end, data['highway'], structure_review(data),
                                   {s: mask.measure(segment) if length > M_EPS else ([], False)
                                    for s, mask in masks.items()})
            ca, cb, highway, review, hits = cache[physical]
            require((ca, cb, highway) == (start, end, data['highway']),
                    f'Inconsistent OSM segment {physical}')
            for s, (spans, touch) in hits.items():
                if a == end:
                    spans = [(length - hi, length - lo) for lo, hi in reversed(spans)]
                intervals[s].extend((offset + lo, offset + hi) for lo, hi in spans)
                touches[s] |= touch
            offset += length
        results[u, v, key] = (structure_review(data),
                              {s: merge_intervals(spans) for s, spans in intervals.items()}, touches)
        spans = results[u, v, key][1]
        if 'S1' in spans and 'S2' in spans:
            for lo, hi in spans['S1']:
                covered = sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in spans['S2'])
                require(hi - lo - covered <= M_EPS, f'S1 road interval outside S2: {data["edge_uid"]}')
        if index % 5000 == 0:
            LOG.info('%s overlay %d/%d directed edges', graph.graph['profile'], index, graph.number_of_edges())
    return results, cache


def split_graph(original, measured, scenario):
    graph = nx.MultiDiGraph(**original.graph, scenario=scenario)
    graph.add_nodes_from(original.nodes(data=True))
    child_rows, impact = [], []
    extra_node = min(0, min(graph.nodes)) - 1
    for u, v, key, data in original.edges(keys=True, data=True):
        line = data['geometry']
        review, spans, touches = measured[u, v, key]
        wet = [(0.0 if a < M_EPS else a, line.length if line.length - b < M_EPS else b)
               for a, b in spans.get(scenario, [])]
        wet_m = sum(b - a for a, b in wet)
        status = 'dry'
        if wet:
            status = 'review' if review else 'blocked'
        elif touches.get(scenario):
            status = 'boundary_contact'
        if status != 'dry':
            impact.append(dict(edge_uid=data['edge_uid'], u=u, v=v, key=key,
                               osmid=data['osmid'], highway=data['highway'], state=status,
                               overlap_m=wet_m, length_m=line.length,
                               extent=('partial' if line.length - wet_m > M_EPS else 'full')
                               if wet else 'boundary_only'))
        cuts = sorted({0.0, line.length, *(x for span in wet for x in span)})
        # Even zero-length source edges retain their OSM topology.
        intervals = list(zip(cuts, cuts[1:])) or [(0.0, line.length)]
        next_u = u
        total_m = 0.0
        for i, (start, end) in enumerate(intervals):
            shape = substring(line, start, end) if len(intervals) > 1 else line
            total_m += shape.length
            midpoint = (start + end) / 2
            submerged = any(a <= midpoint <= b for a, b in wet)
            blocked = submerged and not review
            last = i == len(intervals) - 1
            next_v = v if last else extra_node
            if not last:
                x, y = shape.coords[-1]
                graph.add_node(next_v, x=x, y=y)
                extra_node -= 1
            child_id = f'{data["edge_uid"]}:{i}'
            child = dict(edge_uid=data['edge_uid'], parent_edge_id=data['parent_edge_id'],
                         source_segment_ids=data['source_segment_ids'],
                         source_node_ids=data['source_node_ids'], osmid=data['osmid'],
                         oneway=bool(data['oneway']), reversed=bool(data['reversed']),
                         highway=data['highway'], osm_tags=data['osm_tags'],
                         original_u=u, original_v=v, original_key=key,
                         child_id=child_id, u=next_u, v=next_v,
                         start_m=start, end_m=end, length_m=shape.length,
                         state=('review' if review else 'blocked') if submerged else 'open')
            child_rows.append(child)
            if not blocked:
                graph.add_edge(next_u, next_v, key=child_id, **{
                    k: val for k, val in child.items() if k not in ('u', 'v')
                }, geometry=shape)
            next_u = next_v
        require(abs(total_m - line.length) <= M_EPS, f'Split length mismatch: {data["edge_uid"]}')
    graph.remove_nodes_from(list(nx.isolates(graph)))
    return graph, child_rows, impact


def physical_summary(cache, scenario):
    by_type = defaultdict(Counter)
    for _, _, highway, review, hits in cache.values():
        spans, _ = hits.get(scenario, ([], False))
        length = sum(b - a for a, b in merge_intervals(spans))
        by_type[highway]['physical_overlap_m'] += length
        by_type[highway]['physical_review_m' if review else 'physical_blocked_m'] += length
    return by_type


def component_stats(graph, original):
    result = {}
    for name, function in (('wcc', nx.weakly_connected_components), ('scc', nx.strongly_connected_components)):
        groups = sorted((set(g) for g in function(graph)),
                        key=lambda g: (-len(g & original), -len(g), min(g)))
        ids = {node: i + 1 for i, group in enumerate(groups) for node in group}
        nx.set_node_attributes(graph, ids, name)
        result[f'{name}_count'] = len(groups)
        result[f'largest_{name}_original_nodes'] = len(groups[0] & original) if groups else 0
    return result


def read_facilities(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    transform = Transformer.from_crs(4326, CRS, always_xy=True)
    require(len(rows) == 147, 'Expected 147 archived facilities')
    identities = ['시군명', '시설일련번호', '시설명']
    results = []
    for row in rows:
        if not row['latitude'] and not row['longitude']:
            continue
        xy = transform.transform(float(row['longitude']), float(row['latitude']), errcheck=True)
        require(all(map(math.isfinite, xy)), 'Invalid facility coordinates')
        results.append(({col: row[col] for col in identities}, Point(xy)))
    require(len(results) == 146 and len({(r['시군명'], r['시설일련번호']) for r, _ in results}) == 146,
            'Expected 146 unique geocoded facilities')
    return results


def evaluate_facilities(graph, facilities, anchors):
    eligible, _, _, _ = edge_eligibility(graph, anchors) if graph else ([], [], set(), set())
    indices = []
    for candidates in (graph.edges(keys=True), eligible):
        keys = sorted(k for k in candidates if graph.edges[k]['length_m'] > M_EPS)
        indices.append((keys, shapely.STRtree([graph.edges[k]['geometry'] for k in keys])))
    for identity, point in facilities:
        row = dict(identity)
        for label, (keys, index) in zip(('nearest', 'connected'), indices):
            if keys:
                options, distances = index.query_nearest(point, return_distance=True)
                chosen = min(options, key=lambda i: keys[i])
                edge = graph.edges[keys[chosen]]
                row[f'{label}_distance_m'] = float(distances[0])
                row[f'{label}_child_id'] = edge['child_id']
                row[f'{label}_wcc'] = graph.nodes[keys[chosen][0]]['wcc']
            else:
                row[f'{label}_distance_m'] = None
                row[f'{label}_child_id'] = ''
                row[f'{label}_wcc'] = None
        yield row


def write_csv(path, rows, extra_fields=()):
    fields = list(dict.fromkeys([*extra_fields, *(k for row in rows for k in row)]))
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def export_graph(path, name, graph):
    nodes = [{'node_id': node, 'wcc': d['wcc'], 'scc': d['scc'],
              'geometry': Point(d['x'], d['y'])} for node, d in graph.nodes(data=True)]
    edges = [dict(d, u=u, v=v) for u, v, d in graph.edges(data=True)]
    for suffix, rows, geom_type in (('nodes', nodes, 'Point'), ('edges', edges, 'LineString')):
        frame = gpd.GeoDataFrame(rows or {'geometry': []}, geometry='geometry', crs=CRS)
        layer = f'{name}_{suffix}'
        write_dataframe(frame, path, layer=layer, geometry_type=geom_type)
        info = read_info(path, layer=layer)
        require(info['features'] == len(rows) and info['crs'] == f'EPSG:{CRS}',
                f'Output GeoPackage roundtrip failed: {layer}')


def summarize(graph, source, impact, children, physical):
    original = set(source)
    counts = Counter(row['state'] for row in impact)
    return dict(
        original_nodes_retained=len(original & set(graph)),
        isolated_original_nodes_removed=len(original - set(graph)),
        active_nodes=graph.number_of_nodes(), active_directed_edges=graph.number_of_edges(),
        blocked_directed_parent_edges=counts['blocked'], review_directed_parent_edges=counts['review'],
        boundary_contact_directed_parent_edges=counts['boundary_contact'],
        blocked_directed_child_edges=sum(row['state'] == 'blocked' for row in children),
        directed_overlap_m=sum(row['overlap_m'] for row in impact),
        directed_blocked_m=sum(row['overlap_m'] for row in impact if row['state'] == 'blocked'),
        physical_overlap_m=sum(c['physical_overlap_m'] for c in physical.values()),
        physical_blocked_m=sum(c['physical_blocked_m'] for c in physical.values()),
        **component_stats(graph, original),
    )


def build(args):
    output = args.output.resolve()
    require(not output.exists(), f'Output already exists: {output}')
    require(not output.is_relative_to(args.road_artifact.resolve()), 'Choose output outside road inputs')
    if not args.validate_flood_only:
        road, metadata = road_input_paths(args)
        for path in (road, metadata):
            require(path.is_file(), f'Missing verified road artifact input: {path}')
    LOG.info('Validating flood GeoPackage and archived area report')
    masks, flood_qa = read_flood(args.flood, args.flood_qa)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.flood-road-', dir=output.parent) as temp:
        stage = Path(temp) / 'results'
        stage.mkdir()
        write_json(stage / 'flood_input_validation.json', flood_qa)
        if not args.validate_flood_only:
            analyze(args, stage, masks, flood_qa)
        stage.rename(output)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def draw_map(stage, masks, overlays):
    # The HTML is a presentation layer. All analysis used original mask geometry.
    web = folium.Map(tiles=None, control_scale=True)
    folium.TileLayer('OpenStreetMap').add_to(web)
    for name, geom in masks.items():
        display = gpd.GeoSeries([geom.simplify(10, preserve_topology=True)], crs=CRS).to_crs(4326)
        folium.GeoJson(mapping(display.iloc[0]), name=f'{name} 침수구역', show=name == 'S2',
                       style_function=lambda _: {'color': '#2366b0', 'weight': 1, 'fillOpacity': 0.2}).add_to(web)
        if name == 'S2':
            x1, y1, x2, y2 = display.total_bounds
            web.fit_bounds([[y1, x1], [y2, x2]])
    for name, rows in overlays.items():
        if rows:
            frame = gpd.GeoDataFrame(rows, geometry='geometry', crs=CRS).to_crs(4326)
            folium.GeoJson(frame.__geo_interface__, name=name, show=name.startswith('S2'),
                           style_function=lambda feature: {
                               'color': '#d48500' if feature['properties']['state'] == 'review' else '#b62233',
                               'weight': 2,
                           }).add_to(web)
    folium.LayerControl(collapsed=False).add_to(web)
    web.save(stage / 'flood_road_map.html')


def road_input_paths(args):
    return (args.road_gpkg or args.road_artifact / 'road_network_profiles.gpkg',
            args.road_metadata or args.road_artifact / 'metadata.json')


def analyze(args, stage, masks, flood_qa):
    road, metadata = road_input_paths(args)
    road, road_qa = read_road_metadata(road, metadata)
    facility_hash = file_sha256(args.facilities)
    require(facility_hash == road_qa['verified_facility_sha256'], 'Facility CSV differs from verified artifact input')
    facilities = read_facilities(args.facilities)
    ordinary = read_profile(road, 'drive_reference')
    anchors, anchor_qa = reference_core(ordinary)
    indices = {name: Mask(geometry) for name, geometry in masks.items()}
    summary, road_types, facility_rows = [], [], []
    overlays = defaultdict(list)
    for profile in PROFILES:
        LOG.info('Processing %s', profile)
        source = ordinary if profile == 'drive_reference' else read_profile(road, profile)
        measured, physical_cache = overlay(source, indices)
        baseline = None
        baseline_components = {}
        for scenario in SCENARIOS:
            LOG.info('%s %s exporting scenario', profile, scenario)
            graph, children, impact = split_graph(source, measured, scenario)
            physical = physical_summary(physical_cache, scenario)
            stats = summarize(graph, source, impact, children, physical)
            if scenario == 'S0':
                require(graph.number_of_nodes() == source.number_of_nodes()
                        and graph.number_of_edges() == source.number_of_edges()
                        and set(graph.nodes) == set(source.nodes), 'S0 topology differs from source')
                baseline_components = {n: (graph.nodes[n]['wcc'], graph.nodes[n]['scc']) for n in source}
            fragments = []
            for node, (wcc, scc) in baseline_components.items():
                active = graph.nodes[node] if node in graph else {}
                fragments.append(dict(osm_node=node, s0_wcc=wcc, s0_scc=scc,
                                      scenario_wcc=active.get('wcc'), scenario_scc=active.get('scc')))
            evaluated = list(evaluate_facilities(graph, facilities, anchors))
            if baseline is None:
                baseline = evaluated
            for row, initial in zip(evaluated, baseline, strict=True):
                row.update(profile=profile, scenario=scenario)
                for label in ('nearest', 'connected'):
                    field = f'{label}_distance_m'
                    row[f'{label}_change_from_S0_m'] = (row[field] - initial[field]
                                                        if row[field] is not None and initial[field] is not None
                                                        else None)
            stats['facilities_connected'] = sum(row['connected_distance_m'] is not None for row in evaluated)
            for limit in (50, 100, 300):
                stats[f'facilities_connected_within_{limit}_m'] = sum(
                    row['connected_distance_m'] is not None and row['connected_distance_m'] <= limit
                    for row in evaluated)
            stats['facilities_without_connected_target_since_S0'] = sum(
                row['connected_distance_m'] is None and initial['connected_distance_m'] is not None
                for row, initial in zip(evaluated, baseline, strict=True))
            facility_rows.extend(evaluated)
            summary.append(dict(profile=profile, scenario=scenario, **stats))
            typed = defaultdict(Counter)
            for record in impact:
                count = typed[record['highway']]
                count[f'{record["state"]}_directed_edges'] += 1
                count['directed_overlap_m'] += record['overlap_m']
            road_types.extend(dict(profile=profile, scenario=scenario, highway=highway,
                                   **physical[highway], **typed[highway]) for highway in sorted(physical))
            name = f'{profile}_{scenario.lower()}'
            export_graph(stage / 'road_scenarios.gpkg', name, graph)
            write_csv(stage / f'{name}_edge_trace.csv', children)
            write_csv(stage / f'{name}_node_components.csv', fragments)
            write_csv(stage / f'{name}_road_impact.csv', impact,
                      ('edge_uid', 'u', 'v', 'key', 'osmid', 'highway', 'state', 'overlap_m', 'length_m', 'extent'))
            if profile == 'relief_vehicle_candidate' and scenario != 'S0':
                seen = set()
                for child in children:
                    if child['state'] == 'open':
                        continue
                    original_geom = source.edges[child['original_u'], child['original_v'],
                                                 child['original_key']]['geometry']
                    shape = substring(original_geom, child['start_m'], child['end_m'])
                    identity = (child['osmid'], shapely.normalize(shape).wkb)
                    if identity not in seen:
                        overlays[f'{scenario} 구호차량 후보망'].append(
                            {'state': child['state'], 'geometry': shape})
                        seen.add(identity)
    write_csv(stage / 'scenario_comparison.csv', summary)
    write_csv(stage / 'road_type_comparison.csv', road_types)
    write_csv(stage / 'facility_access_comparison.csv', facility_rows)
    draw_map(stage, masks, overlays)
    require(file_sha256(args.flood) == flood_qa['sha256']
            and file_sha256(args.flood_qa) == flood_qa['area_reference_sha256']
            and file_sha256(road) == road_qa['sha256']
            and file_sha256(metadata) == road_qa['metadata_sha256']
            and file_sha256(args.facilities) == facility_hash, 'An input changed during analysis')
    validation = {
        'status': 'passed', 'analysis_crs': f'EPSG:{CRS}', 'flood': flood_qa, 'roads': road_qa,
        'facility_path': str(args.facilities.resolve()), 'facility_sha256': facility_hash,
        'numerical_length_tolerance_m': M_EPS, 'html_mask_display_tolerance_m': 10,
        'structures_policy': 'positive planar overlap is flagged and left open for review',
        'split_topology': 'direction-specific boundary nodes, no artificial turns',
        'baseline_reference_core': anchor_qa, 'scenarios': summary,
    }
    report = [
        '# 부여·청양 침수 도로망 시나리오', '',
        '국가·지방하천 100년 빈도 위험지도를 결합한 연구용 민감도 분석이다.',
        'S1은 N331~N334, S2는 N330~N334를 적용한다. 도로 길이는 EPSG:5186 평면 기준이다.',
        '교량·고가는 검토 상태로 남겨 계산에서 통과 가능으로 가정했다. 차량 통행 안전 판단에는 현장 확인이 필요하다.',
        '분할점은 방향별로 만들어 원래 도로 접점에서만 방향을 바꿀 수 있도록 했다. 연결요소는 분할 후 열린 방향 간선의 끝점을 기준으로 계산했다.',
        '시설 좌표는 주소 대표점이다. 연결 거리는 기준 도로망의 도달 가능 도로까지의 직선거리이며 실제 진입로나 이동 거리는 별도로 확인해야 한다.',
        '', '| 프로필 | 시나리오 | 차단 방향 간선 | 중복 제거 차단 km | WCC | SCC | 연결 후보 시설 |',
        '|---|---|---:|---:|---:|---:|---:|',
    ]
    for row in summary:
        report.append('| {profile} | {scenario} | {blocked_directed_parent_edges} | {km:.3f} | '
                      '{wcc_count} | {scc_count} | {facilities_connected} |'.format(
                          **row, km=row['physical_blocked_m'] / 1000))
    (stage / 'report_ko.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    validation['code_sha256'] = {
        name: file_sha256(ROOT / 'scripts' / name)
        for name in ('build_flood_road_scenarios.py', 'flood_road_inputs.py', 'connected_access_qa.py')
    }
    validation['output_sha256'] = {p.name: file_sha256(p) for p in stage.iterdir()}
    write_json(stage / 'validation.json', validation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flood', type=Path,
                        default=ROOT / 'local_inputs/flood_depth_100y_result/flood_100y_depth_scenarios.gpkg')
    parser.add_argument('--flood-qa', type=Path, default=ROOT / 'results/flood_100y/flood_depth_qa.json')
    parser.add_argument('--road-artifact', type=Path, default=ROOT / 'local_inputs/road_profile_artifacts')
    parser.add_argument('--road-gpkg', type=Path, help='Explicit path to the verified road profile GeoPackage')
    parser.add_argument('--road-metadata', type=Path, help='Explicit path to the verified artifact metadata.json')
    parser.add_argument('--facilities', type=Path, default=ROOT / FACILITIES)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validate-flood-only', action='store_true')
    args = parser.parse_args()
    default_flood = parser.get_default('flood')
    desktop_flood = Path.home() / 'Desktop/flood_depth_100y_result/flood_100y_depth_scenarios.gpkg'
    if args.flood == default_flood and not default_flood.is_file() and desktop_flood.is_file():
        args.flood = desktop_flood
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        require(args.flood.is_file(), f'Missing verified flood GeoPackage: {args.flood}')
        build(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
