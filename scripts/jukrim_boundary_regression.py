"""Offline local regression for the verified Jukrim boundary/simplification loss.

Uses committed historical local subsets, never the national PBF or an API.
This does not rebuild or write either county's production graph.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
from unittest.mock import patch

import osmnx as ox
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'output/road_network_audit/jukrim/verified_20261112'
TARGETS = (978430790, 978430789, 166680097)


def run_regression():
    if ox.__version__ != '2.1.1':
        raise RuntimeError('This historical regression requires OSMnx 2.1.1')
    attempts = []

    def deny(*args, **kwargs):
        attempts.append('blocked')
        raise AssertionError('Network access forbidden during local regression')

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    paths = [EVIDENCE / 'pbf_results/evidence.json']
    for county in ('cheongyang', 'buyeo'):
        paths.extend([
            EVIDENCE / f'cause/{county}_historical_local_input.json',
            ROOT / f'data/20261109T045434949410Z/{county}/boundary.geojson',
        ])
    before = {path: digest(path) for path in paths}
    evidence = json.loads(paths[0].read_text(encoding='utf-8'))
    roads = {road['way_id']: road for road in evidence['roads']}
    trials = []
    with patch.object(socket.socket, 'connect', deny), \
         patch.object(socket.socket, 'connect_ex', deny), \
         patch('socket.getaddrinfo', deny), \
         patch('requests.sessions.Session.request', deny):
        for county in ('cheongyang', 'buyeo'):
            raw = json.loads((EVIDENCE / f'cause/{county}_historical_local_input.json')
                             .read_text(encoding='utf-8'))
            nodes = {e['id']: e for e in raw['elements'] if e['type'] == 'node'}
            ways = {e['id']: e for e in raw['elements'] if e['type'] == 'way'}
            for way_id in TARGETS:
                old, new = ways[way_id], roads[way_id]
                if old['nodes'] != new['nodes'] or old['tags'] != new['tags']:
                    raise AssertionError(f'Historical way differs: {county}/{way_id}')
                coordinates = [[nodes[n]['lon'], nodes[n]['lat']] for n in old['nodes']]
                if coordinates != new['geometry']['coordinates']:
                    raise AssertionError(f'Historical coordinates differ: {county}/{way_id}')
            boundary = json.loads((ROOT / f'data/20261109T045434949410Z/{county}/boundary.geojson')
                                  .read_text(encoding='utf-8'))
            polygon = shape(boundary['features'][0]['geometry'])
            for simplify, by_edge in ((False, False), (True, False), (True, True)):
                # Patch only the download boundary: use the actual installed pipeline.
                with patch('osmnx.graph._overpass._download_overpass_network',
                           return_value=iter([raw])):
                    graph = ox.graph_from_polygon(
                        polygon, network_type='drive', simplify=simplify,
                        retain_all=True, truncate_by_edge=by_edge)
                counts = {str(way): 0 for way in TARGETS}
                for _, _, attrs in graph.edges(data=True):
                    ids = attrs['osmid']
                    ids = ids if isinstance(ids, list) else [ids]
                    for way in TARGETS:
                        counts[str(way)] += way in ids
                expected = (42 if county == 'cheongyang' else 0, 0)
                if simplify:
                    expected = (2, 2) if by_edge else (0, 0)
                actual = (counts['978430790'], counts['978430789'])
                if actual != expected:
                    raise AssertionError(f'{county}/{simplify}/{by_edge}: {actual} != {expected}')
                endpoint_degree = (graph.degree(11270863949)
                                   if 11270863949 in graph else None)
                if county == 'cheongyang' and simplify and not by_edge and endpoint_degree != 0:
                    raise AssertionError('Expected isolated Cheongyang endpoint')
                trials.append(dict(county=county, simplify=simplify,
                                   truncate_by_edge=by_edge, target_edge_counts=counts,
                                   endpoint_degree=endpoint_degree))
    if attempts or any(digest(path) != checksum for path, checksum in before.items()):
        raise AssertionError('Network attempted or committed input changed')
    return dict(status='passed', osmnx_version=ox.__version__, trials=trials,
                network_attempts=len(attempts), inputs_unchanged=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional new JSON file; never overwrite')
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        parser.error('Output already exists')
    result = run_regression()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output is not None:
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
