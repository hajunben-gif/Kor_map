"""Recover archived road intervals and encode display-only geometry for the browser."""

import base64
import gzip
import json

import geopandas as gpd
from pyproj import Transformer
import shapely
from shapely.geometry import Point
from shapely.ops import substring

from scripts.facility_access_scenarios import (
    PROFILE, S0_LAYER, STATE_RANK, WET_SCENARIOS, Span, check_trace_nesting, read_trace, s0_edges_match,
)
from scripts.flood_road_inputs import CRS, PROFILES, layer, require
from scripts.qa_flood_road_scenarios import SCENARIOS, TOL, normalize, read_saved, unique

ROAD_SIMPLIFY_M = 1.5
MASK_SIMPLIFY_M = 10.0
TO_MAP = Transformer.from_crs(CRS, 3857, always_xy=True)
TO_METRIC = Transformer.from_crs(4326, CRS, always_xy=True)


def packed_json(data):
    plain = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return base64.b64encode(gzip.compress(plain, compresslevel=6, mtime=0)).decode('ascii')


def map_coords(line):
    x, y = TO_MAP.transform(*zip(*line.coords, strict=True), errcheck=True)
    return [round(v, 2) for point in zip(x, y, strict=True) for v in point]


def physical_pairs(edges, ordinals):
    """Pair only opposite directions of the same complete OSM segment chain."""
    exact = {}
    for uid, row in edges.items():
        key = (row['osmid'], ordinals[uid], row['source_node_ids'])
        require(key not in exact, f'{uid}: ambiguous original OSM segment chain')
        exact[key] = uid
    result = []
    seen = set()
    for uid, row in edges.items():
        if uid in seen:
            continue
        mate = None
        if not row['oneway']:
            reversed_nodes = ','.join(reversed(row['source_node_ids'].split(',')))
            other_uid = exact.get((row['osmid'], ordinals[uid][::-1], reversed_nodes))
            if other_uid and other_uid != uid and other_uid not in seen:
                other = edges[other_uid]
                if (not other['oneway'] and other['reversed'] != row['reversed']
                        and list(other['geometry'].coords) == list(reversed(row['geometry'].coords))):
                    mate = other_uid
                    seen.add(other_uid)
        seen.add(uid)
        result.append((uid, mate))
    return result


def reversed_partition_matches(spans, reverse, length):
    return len(spans) == len(reverse) and all(
        a.state == b.state and abs(a.start_m - (length - b.end_m)) <= TOL
        and abs(a.end_m - (length - b.start_m)) <= TOL
        for a, b in zip(spans, reversed(reverse), strict=True)
    )


def road_shapes(edges, pairs, intervals, road_types):
    shapes = []
    duplicates = 0
    for uid, mate in pairs:
        line = edges[uid]['geometry']
        canonical = intervals[uid]
        ordered = [(uid, canonical)]
        if mate:
            if reversed_partition_matches(canonical, intervals[mate], line.length):
                duplicates += len(intervals[mate])
            else:
                ordered.append((mate, intervals[mate]))
        for direction, spans in ordered:
            parent = edges[direction]
            geom = parent['geometry']
            for item in spans:
                if item.end_m <= item.start_m:
                    continue
                # The archived interval is cut before simplifying the display line.
                whole = item.start_m == 0 and item.end_m >= geom.length
                shape = geom if whole else substring(geom, item.start_m, item.end_m)
                shape = shape.simplify(ROAD_SIMPLIFY_M, preserve_topology=False)
                shapes.append([STATE_RANK[item.state], road_types[parent['highway']], map_coords(shape)])
    return shapes, duplicates


def flood_shapes(flood):
    result = {}
    for s in WET_SCENARIOS:
        name = f'scenario_{s.lower()}_closure_zone'
        frame = layer(flood, name, CRS, ('Polygon', 'MultiPolygon'))
        polygons = shapely.get_parts(frame.geometry.simplify(MASK_SIMPLIFY_M, preserve_topology=True).array)
        result[s] = [[map_coords(g.exterior), *(map_coords(r) for r in g.interiors)] for g in polygons]
    return result


def load_geometry(data):
    paths = data['paths']
    nodes = unique(read_saved(paths['s0'], S0_LAYER, 'nodes'), 'node_id')
    edges = unique([normalize(r) for r in read_saved(paths['s0'], S0_LAYER, 'edges')], 'edge_uid')
    require((len(nodes), len(edges)) == PROFILES[PROFILE], 'Unexpected S0 layer dimensions')
    s0_edges_match(nodes, edges)
    sequence = gpd.read_file(paths['road'], layer=f'{PROFILE}_simplified_edges', ignore_geometry=True,
                             columns=['edge_uid', 'osmid', 'source_node_ids', 'raw_segments'])
    base = unique(sequence.to_dict('records'), 'edge_uid')
    require(base.keys() == edges.keys(), 'Original and scenario direction keys differ')
    ordinals = {}
    for uid, row in edges.items():
        record = base[uid]
        ordinals[uid] = tuple(json.loads(record['raw_segments']))
        require(int(record['osmid']) == row['osmid'] and record['source_node_ids'] == row['source_node_ids']
                and len(ordinals[uid]) == len(row['geometry'].coords) - 1, f'{uid}: OSM source chain differs')
    pairs = physical_pairs(edges, ordinals)
    types = sorted({r['highway'] for r in edges.values()})
    road_types = {name: n for n, name in enumerate(types)}
    traces = {'S0': {uid: [Span.from_row(r)] for uid, r in edges.items()}}
    for s in WET_SCENARIOS:
        traces[s], count = read_trace(paths[s], edges, nodes.keys())
        require(data['final_qa']['trace_direction_child_counts'][s] == count
                and count.get('open', 0) + count.get('review', 0) == data['stats'][s]['active_directed_edges']
                and count.get('blocked', 0) == data['stats'][s]['blocked_directed_child_edges'],
                f'{s}: saved trace counts differ')
    check_trace_nesting(traces['S1'], traces['S2'])

    encoded = {}
    display = {}
    for s in SCENARIOS:
        shapes, duplicates = road_shapes(edges, pairs, traces[s], road_types)
        encoded[s] = packed_json(shapes)
        display[s] = {'road_shapes': len(shapes), 'reverse_duplicates_omitted': duplicates}
    bounds = gpd.GeoSeries([row['geometry'] for row in nodes.values()], crs=CRS).to_crs(4326).total_bounds
    west, south, east, north = bounds
    for site in data['sites']:
        issue = site['access_point_id']
        uid = site['s0_edge_uid']
        require(uid in edges, f'{issue}: unknown S0 edge')
        edge = edges[uid]
        road = edge['geometry']
        m = float(site['s0_edge_measure_m'])
        x, y = (float(site[k]) for k in ('final_projection_x_5186', 'final_projection_y_5186'))
        lon, lat = (float(site[k]) for k in ('final_projection_longitude', 'final_projection_latitude'))
        require(0 <= m <= road.length + TOL and edge['highway'] == site['highway']
                and road.interpolate(m).distance(Point(x, y)) <= 0.002
                and Point(x, y).distance(Point(TO_METRIC.transform(lon, lat, errcheck=True))) <= 0.002
                and int(site['s0_direction_count']) == (1 if edge['oneway'] else 2),
                f'{issue}: final position differs from the archived S0 direction')
        f_lon, f_lat = (float(site[k]) for k in ('facility_longitude', 'facility_latitude'))
        west, south, east, north = min(west,f_lon,lon), min(south,f_lat,lat), max(east,f_lon,lon), max(north,f_lat,lat)
    masks = {s: packed_json(rows) for s, rows in flood_shapes(paths['flood']).items()}
    return types, encoded, masks, [[south, west], [north, east]], display
