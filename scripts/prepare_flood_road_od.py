"""Prepare fixed-node OD distances after auditing saved road scenarios."""
import argparse
import csv
import gzip
import json
import math
from pathlib import Path

import networkx as nx

from scripts.build_flood_road_scenarios import read_facilities
from scripts.flood_road_inputs import file_sha256, require
from scripts.qa_flood_road_scenarios import SCENARIOS, TOL, add_input_arguments, audit, csv_rows

PROFILE = 'relief_vehicle_candidate'


def read_entrances(path, facilities, candidates):
    expected = {(r['시군명'], r['시설일련번호']) for r, _ in facilities}
    entries = {}
    for row in csv_rows(path):
        key = row['county'], row['facility_id']
        require(key in expected and key not in entries, f'Unexpected or duplicate facility: {key}')
        status = row['entrance_status']
        node = row['entrance_node_id'].strip()
        if status == 'verified':
            require(node and int(node) in candidates and row['evidence'].strip(),
                    f'{key}: verified entrance needs an S0 node and evidence')
            entries[key] = (int(node), row['evidence'])
        else:
            require(status == 'unverified' and not node, f'{key}: invalid unverified entrance')
            entries[key] = (None, row['evidence'])
    require(entries.keys() == expected, 'Entrance file must explicitly cover every geocoded facility')
    return entries


def routing_view(graph, review_policy):
    return graph.edge_subgraph([
        (u, v, key) for u, v, key, data in graph.edges(keys=True, data=True)
        if review_policy == 'open' or data['state'] == 'open'
    ])


def to_entrance(graph, node):
    if node not in graph or graph.in_degree(node) == 0:
        return {}
    return nx.single_source_dijkstra_path_length(graph.reverse(copy=False), node, weight='length_m')


def check_od(before, after, origin, destination):
    require(after + TOL >= before, f'Shorter S2 OD: {origin} -> {destination}: {before} -> {after}')


def write_od(output, candidates, entries, graphs):
    counts = {n: {s: 0 for s in SCENARIOS} for n in candidates}
    unknown = sum(node is None for node, _ in entries.values())
    with gzip.open(output / 'od.csv.gz', 'wt', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['warehouse_node_id', 'county', 'facility_id', 'entrance_node_id',
                         'scenario', 'network_distance_m', 'reachable', 'status'])
        for (county, facility), (node, _) in sorted(entries.items()):
            distances = {s: to_entrance(graphs[s], node) if node is not None else {} for s in SCENARIOS}
            for candidate in candidates:
                values = {s: distances[s].get(candidate, math.inf) for s in SCENARIOS}
                if node is not None:
                    check_od(values['S1'], values['S2'], candidate, (county, facility))
                for scenario, distance in values.items():
                    reachable = math.isfinite(distance)
                    status = 'reachable' if reachable else 'unreachable'
                    if node is None:
                        status = 'entrance_unverified'
                    writer.writerow([candidate, county, facility, node, scenario, distance if reachable else '',
                                     int(reachable) if node is not None else '', status])
                    counts[candidate][scenario] += int(reachable)
    with (output / 'unreachable_counts.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['warehouse_node_id', 'scenario', 'reachable_count', 'unreachable_count',
                         'entrance_unverified_count'])
        for candidate, scenarios in counts.items():
            for scenario, reachable in scenarios.items():
                writer.writerow([candidate, scenario, reachable, len(entries) - unknown - reachable, unknown])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_input_arguments(parser)
    parser.add_argument('--entrances', required=True, type=Path)
    parser.add_argument('--review-policy', choices=('closed', 'open'), default='closed')
    args = parser.parse_args()
    require(not args.output.resolve().is_relative_to(args.run.resolve()), 'Output must be outside the run')
    graphs = {}
    candidates = {}

    def collect(profile, scenario, source, graph):
        if profile == PROFILE:
            graphs[scenario] = routing_view(graph, args.review_policy)
            if scenario == 'S0':
                candidates.update({node: (data['x'], data['y']) for node, data in source.nodes(data=True)})

    qa = audit(args, on_graph=collect)
    facility_hash = file_sha256(args.facilities)
    facilities = read_facilities(args.facilities)
    entrance_hash = file_sha256(args.entrances)
    entries = read_entrances(args.entrances, facilities, candidates)
    require(facility_hash == file_sha256(args.facilities) == qa['facility_sha256']
            and entrance_hash == file_sha256(args.entrances), 'OD input changed')
    require(any(node is not None for node, _ in entries.values()), 'No verified entrances to route')
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / 'warehouse_nodes.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['warehouse_node_id', 'x_5186', 'y_5186'])
        writer.writerows((node, *candidates[node]) for node in sorted(candidates))
    write_od(args.output, sorted(candidates), entries, graphs)
    (args.output / 'od_manifest.json').write_text(json.dumps(dict(
        status='passed', profile=PROFILE, scenarios=SCENARIOS, review_policy=args.review_policy,
        warehouse_nodes=len(candidates), demand_facilities=len(entries),
        verified_entrances=sum(node is not None for node, _ in entries.values()),
        entrances_sha256=entrance_hash, facilities_sha256=facility_hash,
        road_scenarios_sha256=qa['run_gpkg_sha256'], od_monotonicity_tolerance_m=TOL,
    ), indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
