"""Build and validate a directed, traceable road graph from one OSM snapshot."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import chain, pairwise
import json
import math

import networkx as nx
import osmium
import osmnx as ox
from pyproj import CRS
import shapely
from shapely.geometry import LineString, Point

from scripts.analyze_road_access import TO_METRES, wcc_index
from scripts.jukrim_pbf_audit import audit_module, osmium_path

DRIVE = ox._overpass._get_network_filter("drive")
TARGETS = (978430790, 978430789, 166680097)
CONTROL_NODES = (11270863949, 9053552621, 1781315489)
WAY_FIELDS = ("highway", "name", "ref", "bridge", "tunnel", "layer", "junction",
              "access", "vehicle", "motor_vehicle", "motorcar", "hgv", "maxweight",
              "maxheight", "maxwidth", "surface", "service")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def metric_line(coords):
    x, y = TO_METRES.transform(*zip(*coords), errcheck=True)
    return LineString(zip(x, y))


def drive_rejections(roads):
    reject = audit_module().filter_rejections
    return {wid: reject(road["tags"], DRIVE) for wid, road in roads.items()}


def read_pbf(source, area, scratch):
    """Include full ways with at least one segment touching the metric polygon."""
    selected = {}
    counts = Counter()
    shapely.prepare(area)
    bounds = area.bounds
    storage = f"sparse_file_array,{scratch / 'node-locations'}"
    processor = osmium.FileProcessor(osmium_path(source), osmium.osm.NODE | osmium.osm.WAY)
    processor = processor.with_locations(storage).with_filter(osmium.filter.KeyFilter("highway"))
    for way in processor:
        if not way.is_way():
            continue
        counts["highway_ways_scanned"] += 1
        refs = [node.ref for node in way.nodes]
        coords = [(node.lon, node.lat) if node.location.valid() else None for node in way.nodes]
        if not refs or None in coords:
            raise ValueError(f"Highway {way.id} has missing references; area coverage is unknown")
        if len(refs) == 1:
            if area.covers(Point(TO_METRES.transform(*coords[0], errcheck=True))):
                raise ValueError(f"Incomplete highway {way.id} inside analysis area")
            counts["one_node_highways_outside"] += 1
            continue
        line = metric_line(coords)
        x1, y1, x2, y2 = line.bounds
        if x2 < bounds[0] or x1 > bounds[2] or y2 < bounds[1] or y1 > bounds[3]:
            continue
        if area.intersects(line) or (line.length == 0 and area.covers(Point(line.coords[0]))):
            if way.id in selected:
                raise ValueError(f"Multiple versions of way {way.id} in snapshot")
            selected[way.id] = dict(nodes=refs, coords=coords, tags=dict(way.tags))
    if not selected:
        raise ValueError("No highways intersect the study area")
    counts["intersecting_highway_ways"] = len(selected)
    return selected, dict(counts)


def outside_links(source, selected, drive_nodes, area):
    """Audit source drive neighbors omitted at the outer perimeter and retain node tags."""
    reject = audit_module().filter_rejections
    outside = defaultdict(set)
    tags = {}
    processor = osmium.FileProcessor(osmium_path(source), osmium.osm.NODE | osmium.osm.WAY)
    for obj in processor:
        if obj.is_node():
            if obj.id in drive_nodes and obj.tags:
                tags[obj.id] = dict(obj.tags)
            continue
        if obj.id in selected or "highway" not in obj.tags:
            continue
        refs = {ref.ref for ref in obj.nodes} & drive_nodes.keys()
        if refs and not reject(dict(obj.tags), DRIVE):
            for node in refs:
                if area.covers(Point(drive_nodes[node])):
                    raise ValueError(f"Drive way {obj.id} omitted at interior node {node}")
                outside[node].add(obj.id)
    return {node: sorted(ways) for node, ways in sorted(outside.items())}, tags


def is_oneway(tags):
    return ox.graph._is_path_one_way(tags, False, {"yes", "true", "1", "-1", "reverse", "T", "F"})


def build_graphs(roads, rejected, area, counties, source=None):
    raw = nx.MultiDiGraph(crs="EPSG:4326", simplified=False, network_type="drive",
                          retain_all=True, source="OpenStreetMap contributors / ODbL 1.0")
    # The selected and stored edge sets are checked using raw direction, original
    # segment ordinal and way ID, even when different ways share the same nodes.
    expected = Counter()
    physical = {}
    drive_nodes = {}
    internal = {}
    outer = {}
    nonmoving_ways = []
    keep = set(CONTROL_NODES)
    boundary = area.boundary
    shapely.prepare(boundary)
    for county in counties.values():
        shapely.prepare(county)
    for wid, road in roads.items():
        if rejected[wid]:
            continue
        refs, coords, tags = road["nodes"], road["coords"], road["tags"]
        if len(refs) != len(coords) or len(refs) < 2:
            raise ValueError(f"Invalid coordinates for way {wid}")
        line = metric_line(coords)
        projected = list(line.coords)
        if not (area.intersects(line) or (line.length == 0 and area.covers(Point(projected[0])))):
            raise ValueError(f"Selected drive way {wid} lies outside analysis area")
        if all(county.intersects(line) and line.intersection(county).length > 0
               for county in counties.values()):
            internal[wid] = len(refs)
        if boundary.intersects(line):
            covered = [area.covers(Point(xy)) for xy in projected]
            outer[wid] = dict(nodes=len(refs), nodes_outside=covered.count(False),
                              all_vertices_outside=not any(covered),
                              extends_outside=not area.covers(line))
        if len(set(refs)) == 1:
            nonmoving_ways.append(wid)
        keep.update((refs[0], refs[-1]))
        for node, (x, y), metric in zip(refs, coords, projected):
            if node in raw and (x, y) != (raw.nodes[node]["x"], raw.nodes[node]["y"]):
                raise ValueError(f"Conflicting source coordinates for node {node}")
            raw.add_node(node, x=x, y=y)
            drive_nodes[node] = metric
        # Same OSMnx 2.1.1 orientation rules as _add_paths, with segment identity
        # retained even for repeated node references or parallel edges.
        one_way = is_oneway(tags)
        backward = one_way and ox.graph._is_path_reversed(tags, {"-1", "reverse", "T"})
        attrs = {key: tags[key] for key in WAY_FIELDS if key in tags}
        attrs.update(osmid=wid, oneway=one_way, osm_tags=json_text(tags))
        for i, (u, v) in enumerate(pairwise(refs)):
            if u == v:  # Repeated consecutive references have no OSM movement.
                continue
            length = math.dist(projected[i], projected[i + 1])
            physical[wid, i] = length
            forward = (v, u) if backward else (u, v)
            directions = [(*forward, False)]
            if not one_way:
                directions.append((v, u, True))
            for a, b, reverse_edge in directions:
                expected[a, b, wid, i] += 1
                raw.add_edge(a, b, **attrs, reversed=reverse_edge,
                             raw_segments=[i], length_5179=length)
    raw.remove_nodes_from(list(nx.isolates(raw)))
    if not raw.number_of_edges() or not nx.is_directed(raw) or not raw.is_multigraph():
        raise ValueError("Expected a nonempty directed MultiDiGraph")
    if CRS.from_user_input(raw.graph["crs"]).to_epsg() != 4326:
        raise ValueError("Expected original EPSG:4326 graph")
    drive_nodes = {node: drive_nodes[node] for node in raw}
    if source is None:
        omitted, node_tags = {}, {}
    else:
        omitted, node_tags = outside_links(source, roads, drive_nodes, area)
    keep.update(omitted)
    for node, tags in node_tags.items():
        raw.nodes[node]["osm_tags"] = json_text(tags)
        keep.add(node)
    for u, neighbors in raw.adjacency():
        for v, edges in neighbors.items():
            if len(edges) > 1:
                keep.update((u, v))
    nx.set_node_attributes(raw, {node: True for node in keep if node in raw}, "keep_osm_node")
    ox.distance.add_edge_lengths(raw)
    simple = ox.simplify_graph(
        raw, node_attrs_include=["keep_osm_node"], edge_attrs_differ=["osmid"],
        remove_rings=False, edge_attr_aggs={
            "length": sum, "length_5179": sum,
            "raw_segments": lambda segments: list(chain.from_iterable(segments)),
        },
    )
    for graph in (raw, simple):
        for u, v, attrs in graph.edges(data=True):
            if "geometry" not in attrs:
                attrs["geometry"] = LineString([(raw.nodes[n]["x"], raw.nodes[n]["y"]) for n in (u, v)])
    metrics = validate_graphs(raw, simple, roads, expected, physical)
    metrics["drive_ways_overlapping_both_counties"] = internal
    metrics["drive_ways_touching_outer_boundary"] = outer
    metrics["drive_way_ids_with_no_distinct_source_nodes"] = sorted(nonmoving_ways)
    metrics["source_external_neighbors_at_retained_nodes"] = {
        str(node): {"omitted_drive_ways": ways,
                    "retained_adjacent_nodes": sorted(set(raw.predecessors(node)) | set(raw.successors(node))),
                    "retained_in_degree": raw.in_degree(node), "retained_out_degree": raw.out_degree(node)}
        for node, ways in omitted.items()
    }
    return raw, simple, metrics


WAY_AUDIT_FIELDS = (
    "osm_way_id", "highway", "source_node_references", "source_segments", "expected_directed_segments",
    "raw_directed_segments", "final_directed_segments", "status", "drive_filter_exclusions",
    "drive_way_overlaps_both_counties", "drive_way_touches_outer_boundary",
    "drive_way_all_vertices_outside",
)


def audit_source_ways(roads, rejected, raw, simplified, checks):
    """Expose source-to-final coverage after geometry and segment identity checks."""
    counts = []
    for graph in (raw, simplified):
        tally = Counter()
        for _, _, attrs in graph.edges(data=True):
            tally[attrs["osmid"]] += len(attrs["raw_segments"])
        counts.append(tally)
    raw_counts, final_counts = counts
    if (raw_counts.keys() | final_counts.keys()) - roads.keys():
        raise ValueError("Graph contains a way absent from the source census")

    outer = checks["drive_ways_touching_outer_boundary"]
    internal = checks["drive_ways_overlapping_both_counties"]
    rows = []
    for way_id, road in sorted(roads.items()):
        segments = sum(a != b for a, b in pairwise(road["nodes"]))
        exclusions = rejected[way_id]
        expected = 0 if exclusions else segments * (1 if is_oneway(road["tags"]) else 2)
        if raw_counts[way_id] != expected or final_counts[way_id] != expected:
            raise ValueError(f"Directed segment coverage differs from the source way {way_id}")
        status = "filtered" if exclusions else "preserved" if segments else "no_distinct_nodes"
        rows.append(dict(zip(WAY_AUDIT_FIELDS, (
            way_id, road["tags"]["highway"], len(road["nodes"]), segments, expected,
            raw_counts[way_id], final_counts[way_id], status, json_text(exclusions),
            "" if exclusions else way_id in internal, "" if exclusions else way_id in outer,
            outer.get(way_id, {}).get("all_vertices_outside", ""),
        ), strict=True)))
    return rows


def component_partition(graph, retained, components):
    return {frozenset(group & retained) for group in components(graph) if group & retained}


def validate_graphs(raw, simple, roads, expected, physical):
    summaries = {}
    retained = set(simple)
    for name, graph in (("raw", raw), ("simplified", simple)):
        observed = Counter()
        seen_ways = set()
        for u, v, attrs in graph.edges(data=True):
            way = attrs["osmid"]
            refs = roads[way]["nodes"]
            current = u
            original_nodes = [u]
            for segment in attrs["raw_segments"]:
                a, b = refs[segment:segment + 2]
                if current not in (a, b):
                    raise ValueError(f"OSM segment chain breaks in way {way}")
                following = b if current == a else a
                observed[current, following, way, segment] += 1
                current = following
                original_nodes.append(current)
            xs_ys = [(raw.nodes[n]["x"], raw.nodes[n]["y"]) for n in original_nodes]
            if current != v or list(attrs["geometry"].coords) != xs_ys:
                raise ValueError(f"Geometry or topology changed in way {way}")
            if attrs["osm_tags"] != json_text(roads[way]["tags"]):
                raise ValueError(f"Source tags changed in way {way}")
            seen_ways.add(way)
        if observed != expected:
            raise ValueError(f"{name}: directed OSM segment coverage changed")
        summaries[name] = dict(
            nodes=graph.number_of_nodes(), directed_edges=graph.number_of_edges(),
            physical_segments=len(physical), ways=len(seen_ways),
            zero_length_source_segments=sum(length == 0 for length in physical.values()),
            directed_length_m=sum(d["length_5179"] for _, _, d in graph.edges(data=True)),
            undirected_length_m=sum(physical.values()),
            weak_components=nx.number_weakly_connected_components(graph),
            strong_components=nx.number_strongly_connected_components(graph),
        )
    for components in (nx.weakly_connected_components, nx.strongly_connected_components):
        if component_partition(raw, retained, components) != component_partition(simple, retained, components):
            raise ValueError("Simplification changed retained-node component partition")
    if summaries["raw"]["weak_components"] != summaries["simplified"]["weak_components"]:
        raise ValueError("Simplification lost a weak component")
    return dict(statistics=summaries, original_directed_segments_preserved=True,
                retained_node_wcc_and_scc_partitions_preserved=True)


def strong_index(graph):
    groups = sorted(nx.strongly_connected_components(graph), key=lambda group: (-len(group), min(group)))
    return {node: (f"integrated_scc_{rank:03d}", len(group))
            for rank, group in enumerate(groups, 1) for node in group}


def jukrim_links(graph):
    edges = {way: [] for way in TARGETS}
    for u, v, key, attrs in graph.edges(keys=True, data=True):
        if attrs["osmid"] in edges:
            edges[attrs["osmid"]].append((u, v, key))
    target = graph.edge_subgraph(list(chain.from_iterable(edges.values())))
    members = {way: {node for u, v, _ in edge_keys for node in (u, v)}
               for way, edge_keys in edges.items()}
    start, junction, trunk = CONTROL_NODES
    wcc = wcc_index(graph, "integrated")[0]
    scc = strong_index(graph)
    pairs = ((start, junction), (junction, trunk), (start, trunk))
    return dict(
        edges_by_way={way: sorted(keys) for way, keys in edges.items()},
        access_bridge_shared_osm_nodes=sorted(members[TARGETS[0]] & members[TARGETS[1]]),
        bridge_trunk_shared_osm_nodes=sorted(members[TARGETS[1]] & members[TARGETS[2]]),
        components={node: dict(wcc=wcc[node], scc=scc[node]) for node in CONTROL_NODES if node in graph},
        trunk_contact_movements={
            "enter_trunk_edges": sorted(edge for edge in edges[TARGETS[2]] if edge[0] == trunk),
            "leave_trunk_edges": sorted(edge for edge in edges[TARGETS[2]] if edge[1] == trunk),
        },
        directed_reachability={
            f"{a}->{b}": a in target and b in target and nx.has_path(target, a, b)
            for pair in pairs for a, b in (pair, pair[::-1])
        },
    )
