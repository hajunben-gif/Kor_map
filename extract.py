"""Real OSM driving networks for Buyeo and Cheongyang (OSMnx 2.x)."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
import folium
import geopandas as gpd
import networkx as nx
import osmnx as ox
import requests

REGIONS = {"buyeo": "부여군", "cheongyang": "청양군"}
LOG = logging.getLogger(__name__)


def retry(fn, attempts=3):
    for attempt in range(attempts):
        try:
            return fn()
        except (requests.RequestException, ox._errors.InsufficientResponseError,
                ox._errors.ResponseStatusCodeError) as exc:
            if attempt + 1 == attempts:
                raise
            response = getattr(exc, "response", None)
            header = response.headers.get("Retry-After", "") if response is not None else ""
            delay = float(header) if header.isdigit() else 15 * 2 ** attempt
            LOG.warning("API 실패 %s/%s: %s; %.0f초 후 재시도", attempt + 1, attempts, exc, delay)
            time.sleep(delay)


def boundary(region, directory, attempts=3):
    name = REGIONS[region]
    local = directory / f"{region}.geojson"
    if local.exists():
        frame = gpd.read_file(local)
        if frame.crs is None:
            raise ValueError(f"{local}: CRS가 필요합니다")
        frame = frame.to_crs(4326)
        source = {"type": "user_polygon", "path": str(local),
                  "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
    else:
        frame = retry(lambda: ox.geocode_to_gdf(
            {"county": name, "state": "충청남도", "country": "대한민국"}), attempts)
        row = frame.iloc[0]
        if (row.get("class") != "boundary" or row.get("type") != "administrative"
                or row.get("osm_type") != "relation" or not any(label in row.get("display_name", "") for label in
                           (name, {"buyeo": "Buyeo-gun", "cheongyang": "Cheongyang-gun"}[region]))):
            raise ValueError(f"{name}: 행정경계 검증 실패. {local}에 정확한 경계를 제공하세요")
        source = {"type": "Nominatim administrative relation", "osm_id": int(row.osm_id),
                  "display_name": row.display_name}
    polygon = frame.geometry.union_all()
    if polygon.geom_type not in ("Polygon", "MultiPolygon") or polygon.is_empty or not polygon.is_valid:
        raise ValueError(f"{name}: 유효한 Polygon/MultiPolygon 경계가 필요합니다")
    # A coarse sanity check only; administrative identity still requires the above checks.
    x1, y1, x2, y2 = polygon.bounds
    if not (125 < x1 < x2 < 128 and 35 < y1 < y2 < 38):
        raise ValueError("충남 지역 범위를 벗어난 경계입니다")
    return polygon, source


def statistics(graph):
    undirected = ox.convert.to_undirected(graph)
    return {"nodes": graph.number_of_nodes(), "directed_edges": graph.number_of_edges(),
            "directed_length_m": sum(float(d["length"]) for *_, d in graph.edges(data=True)),
            "undirected_length_m": sum(float(d["length"]) for *_, d in undirected.edges(data=True)),
            "weak_components": nx.number_weakly_connected_components(graph),
            "strong_components": nx.number_strongly_connected_components(graph)}


def export(graph, polygon, destination, metadata):
    destination.mkdir(parents=True, exist_ok=False)
    graph.graph.update(extracted_at_utc=metadata["extracted_at_utc"],
                       source="OpenStreetMap / ODbL", network_type="drive")
    ox.save_graphml(graph, destination / "network.graphml")
    # directed=True preserves u/v/key and the two directions of bidirectional roads.
    ox.io.save_graph_geopackage(graph, destination / "network.gpkg", directed=True)
    gpd.GeoDataFrame({"region": [metadata["region"]]}, geometry=[polygon], crs=4326).to_file(
        destination / "boundary.geojson", driver="GeoJSON")
    _, edges = ox.graph_to_gdfs(graph)
    display = edges.reset_index()
    fields = [c for c in ["u", "v", "key", "osmid", "length", "highway", "oneway", "name"] if c in display]
    # OSMnx simplified edges can contain list-valued attributes. JSON makes them readable.
    for field in fields:
        display[field] = display[field].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v))
    center = polygon.centroid
    map_ = folium.Map(location=[center.y, center.x], zoom_start=11, tiles="OpenStreetMap", prefer_canvas=True)
    folium.GeoJson(gpd.GeoSeries([polygon], crs=4326).__geo_interface__, name="행정경계",
                   style_function=lambda _: {"fillOpacity": 0, "color": "#222", "weight": 2}).add_to(map_)
    folium.GeoJson(display[fields + ["geometry"]].to_json(), name="자동차 도로",
                   style_function=lambda _: {"color": "#176ab6", "weight": 2},
                   tooltip=folium.GeoJsonTooltip(fields=fields)).add_to(map_)
    map_.fit_bounds([[polygon.bounds[1], polygon.bounds[0]], [polygon.bounds[3], polygon.bounds[2]]])
    folium.LayerControl().add_to(map_)
    map_.save(str(destination / "map.html"))
    (destination / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", nargs="+", choices=REGIONS, default=list(REGIONS))
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--boundaries", type=Path, default=Path("boundaries"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if args.attempts < 1 or args.timeout < 1:
        parser.error("attempts와 timeout은 양수여야 합니다")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(args.output / "cache")
    ox.settings.requests_timeout = args.timeout
    ox.settings.overpass_rate_limit = True
    ox.settings.log_console = True
    run = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run.mkdir(parents=True)
    failures = {}
    for region in args.regions:
        try:
            started = datetime.now(timezone.utc).isoformat()
            polygon, source = boundary(region, args.boundaries, args.attempts)
            graph = retry(lambda: ox.graph_from_polygon(polygon, network_type="drive", simplify=True,
                          retain_all=True, truncate_by_edge=False), args.attempts)
            if not isinstance(graph, nx.MultiDiGraph) or not graph.number_of_edges():
                raise ValueError("비어 있거나 방향성 다중 그래프가 아닌 결과")
            metadata = {"region": REGIONS[region], "request_started_at_utc": started,
                        "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
                        "source": "https://www.openstreetmap.org", "license": "ODbL 1.0",
                        "boundary": source, "crs": str(graph.graph["crs"]),
                        "length_unit": "metre", "network_type": "drive", "retain_all": True,
                        "simplify": True, "truncate_by_edge": False, "osmnx_version": ox.__version__,
                        "overpass_url": ox.settings.overpass_url,
                        "temporal_note": "Extraction time, not a guaranteed historical OSM snapshot; cache may predate this run.",
                        "statistics": statistics(graph)}
            export(graph, polygon, run / region, metadata)
            print(REGIONS[region], json.dumps(metadata["statistics"], ensure_ascii=False), flush=True)
        except Exception as exc:
            LOG.exception("%s 처리 실패", REGIONS[region])
            failures[region] = {"error_type": type(exc).__name__, "message": str(exc)}
    (run / "run_status.json").write_text(json.dumps({"failures": failures}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"결과: {run.resolve()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
