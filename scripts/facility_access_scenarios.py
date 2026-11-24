"""Read existing flood cuts and describe one-way arrival links at fixed road positions."""

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
import math
from pathlib import Path

import shapely
from shapely.geometry import LineString

from scripts.flood_road_inputs import CRS, layer, require
from scripts.qa_flood_road_scenarios import TOL, TRACE_FIELDS, covered, merge, normalize, review_structure

PROFILE = 'relief_vehicle_candidate'
S0_LAYER = f'{PROFILE}_s0'
STATE_RANK = {'open': 0, 'review': 1, 'blocked': 2}
WET_SCENARIOS = ('S1', 'S2')


@dataclass(frozen=True, slots=True)
class Span:
    child_id: str
    u: int
    v: int
    start_m: float
    end_m: float
    length_m: float
    state: str

    @classmethod
    def from_row(cls, row):
        return cls(*(row[field] for field in cls.__dataclass_fields__))


def s0_edges_match(nodes, edges):
    """Check that every saved S0 child exactly spans its directed parent."""
    for uid, edge in edges.items():
        line = edge['geometry']
        first = edge['u']
        last = edge['v']
        require(line.geom_type == 'LineString' and not line.is_empty
                and first in nodes and last in nodes, f'{uid}: invalid S0 geometry or node')
        require(edge['state'] == 'open' and edge['child_id'] == f'{uid}:0'
                and (first, last) == (edge['original_u'], edge['original_v'])
                and abs(edge['start_m']) <= TOL and abs(edge['end_m'] - line.length) <= TOL
                and abs(edge['length_m'] - line.length) <= TOL
                and nodes[first]['geometry'].distance(shapely.get_point(line, 0)) <= 1e-5
                and nodes[last]['geometry'].distance(shapely.get_point(line, -1)) <= 1e-5,
                f'{uid}: S0 child differs from parent chain')


def read_trace(path, parents, original_nodes):
    """Validate lineage and the partition of every direction, including closed children."""
    by_parent = defaultdict(list)
    state_counts = Counter()
    with path.open(encoding='utf-8-sig', newline='') as source:
        for raw in csv.DictReader(source):
            row = normalize(raw)
            uid = row['edge_uid']
            require(uid in parents, f'{path.name}: unknown parent {uid}')
            base = parents[uid]
            require(all(row[f] == base[f] for f in TRACE_FIELDS)
                    and all(row[f] == base[f] for f in ('original_u', 'original_v', 'original_key')),
                    f'{path.name}: S0 lineage mismatch for {row["child_id"]}')
            expected_wet = 'review' if review_structure(base['osm_tags']) else 'blocked'
            require(row['state'] in ('open', expected_wet),
                    f'{path.name}: invalid state for {row["child_id"]}')
            by_parent[uid].append(Span.from_row(row))
            state_counts[row['state']] += 1
    require(by_parent.keys() == parents.keys(), f'{path.name}: missing S0 parent')
    cuts = set()
    for uid, spans in by_parent.items():
        base = parents[uid]
        spans.sort(key=lambda part: (part.start_m, part.end_m))
        last_end, last_node = 0.0, base['u']
        parent_length = base['geometry'].length
        for i, part in enumerate(spans):
            require(part.child_id == f'{uid}:{i}' and part.u == last_node
                    and abs(part.start_m - last_end) <= TOL
                    and 0 <= part.start_m <= part.end_m <= parent_length + TOL
                    and (part.start_m < part.end_m or (parent_length == 0 and len(spans) == 1))
                    and part.length_m >= 0
                    and abs(part.end_m - part.start_m - part.length_m) <= TOL,
                    f'{path.name}: invalid partition {part.child_id}')
            if i + 1 == len(spans):
                require(part.v == base['v'] and abs(part.end_m - parent_length) <= TOL,
                        f'{path.name}: invalid parent terminus {part.child_id}')
            else:
                require(part.v not in original_nodes and part.v not in cuts,
                        f'{path.name}: shared artificial junction {part.v}')
                cuts.add(part.v)
            last_end, last_node = part.end_m, part.v
    return by_parent, dict(state_counts)


def check_trace_nesting(earlier, later):
    """No S1 blocked/review interval may reopen in S2."""
    require(earlier.keys() == later.keys(), 'S1/S2 parent sets differ')
    for uid, spans in earlier.items():
        for state in ('blocked', 'review'):
            previous = merge((p.start_m, p.end_m) for p in spans if p.state == state)
            current = merge((p.start_m, p.end_m) for p in later[uid] if p.state == state)
            require(covered(previous, current), f'{uid}: S1 {state} interval reopens in S2')


def arrival_at_measure(spans, measure):
    """Preserve measured positions at cuts and only attach toward a demand sink."""
    touching = [p for p in spans if p.start_m - TOL <= measure <= p.end_m + TOL]
    require(touching, 'Access point does not occur in trace partition')
    state = max((p.state for p in touching), key=STATE_RANK.__getitem__)
    partial = 0.0
    chosen = touching[0]
    ends = [(p.start_m, p.u) for p in touching if abs(p.start_m - measure) <= TOL]
    ends += [(p.end_m, p.v) for p in touching if abs(p.end_m - measure) <= TOL]
    at_node = bool(ends)
    if state == 'blocked':
        tail, partial_start, partial_end, partial = None, None, None, None
    elif at_node:
        require(len({node for _, node in ends}) == 1, 'Ambiguous arrival node at a trace cut')
        partial_start, tail = ends[0]
        partial_end = partial_start
    else:
        require(len(touching) == 1, 'Ambiguous interior point in trace')
        tail = chosen.u
        partial_start, partial_end = chosen.start_m, measure
        partial = partial_end - partial_start
        require(0 < partial < chosen.length_m + TOL, 'Invalid partial arrival length')
    return dict(
        point_state=state, touches_internal_cut=len(touching) > 1,
        touching_child_ids=';'.join(p.child_id for p in touching),
        arrival_node_id=tail, uses_existing_scenario_node=at_node and state != 'blocked',
        arrival_child_id=chosen.child_id if state != 'blocked' else None,
        arrival_child_start_m=chosen.start_m if state != 'blocked' else None,
        arrival_child_end_m=chosen.end_m if state != 'blocked' else None,
        arrival_child_length_m=chosen.length_m if state != 'blocked' else None,
        arrival_length_m=partial, partial_start_m=partial_start, partial_end_m=partial_end,
        arrival_allowed_review_closed=state == 'open', arrival_allowed_review_open=state != 'blocked',
    )


def read_masks(path: Path):
    """Index archived masks without regenerating them or clipping any road network."""
    trees = {}
    for scenario in WET_SCENARIOS:
        name = f'scenario_{scenario.lower()}_closure_zone'
        mask = layer(path, name, CRS, ('Polygon', 'MultiPolygon'))
        parts = shapely.get_parts(mask.geometry.array)
        shapely.prepare(parts)
        trees[scenario] = shapely.STRtree(parts)
    return trees


def point_in_mask(point, tree):
    hits = tree.query(point, predicate='intersects')
    if not len(hits):
        return 'outside'
    return 'inside' if any(tree.geometries[i].contains(point) for i in hits) else 'boundary'


def connector_in_mask(facility, access, tree):
    """Measure positive interior line overlap separately from boundary contact."""
    if facility.distance(access) <= TOL:
        return 'zero_length', 0.0
    line = LineString((facility, access))
    hits = tree.query(line, predicate='intersects')
    if not len(hits):
        return 'outside', 0.0
    overlaps = []
    for i in hits:
        part = tree.geometries[i]
        clipped = line.intersection(part)
        if clipped.length > TOL:
            if not part.contains_properly(line):
                clipped = clipped.difference(part.boundary)
            if clipped.length > TOL:
                overlaps.append(clipped)
    distance = float(shapely.union_all(overlaps).length)
    require(math.isfinite(distance) and distance <= line.length + TOL, 'Invalid connector overlap')
    if not distance:
        return 'boundary_contact', 0.0
    state = 'fully_overlapped' if abs(distance - line.length) <= TOL else 'partially_overlapped'
    return state, distance
