"""Validate exported real networks: python validate.py data/<run>."""
import json
from pathlib import Path
import sys
import geopandas as gpd
import networkx as nx
import osmnx as ox


def validate(directory):
    graph = ox.load_graphml(directory / 'network.graphml')
    assert isinstance(graph, nx.MultiDiGraph) and graph.number_of_edges() > 0
    nodes = gpd.read_file(directory / 'network.gpkg', layer='nodes')
    edges = gpd.read_file(directory / 'network.gpkg', layer='edges')
    assert nodes.crs.to_epsg() == edges.crs.to_epsg() == 4326
    assert set(map(int, nodes['osmid'])) == set(graph.nodes)
    assert set(zip(edges.u, edges.v, edges.key)) == set(graph.edges(keys=True))
    assert edges.geometry.notna().all() and (edges['length'].astype(float) > 0).all()
    metadata = json.loads((directory / 'metadata.json').read_text())
    assert metadata['statistics']['nodes'] == graph.number_of_nodes()
    assert metadata['statistics']['directed_edges'] == graph.number_of_edges()
    assert (directory / 'map.html').stat().st_size > 1000
    print(directory.name, 'PASS', graph.number_of_nodes(), graph.number_of_edges())


if __name__ == '__main__':
    root = Path(sys.argv[1])
    status = json.loads((root / 'run_status.json').read_text())
    assert not status['failures'], status['failures']
    directories = [p for p in root.iterdir() if (p / 'network.graphml').exists()]
    assert directories, '검증할 그래프가 없습니다'
    for directory in directories:
        validate(directory)
