"""Generate a read-only, local-openable HTML viewer of saved, verified road profiles.

The HTML is a visualization, not a new routing calculation or a road-access permit.
It embeds verified road geometries, county boundaries and facility points.
OpenFreeMap vector basemap uses documented public service, not blocked OSM raster tiles.
Leaflet, MapLibre, vector tiles and styles need an internet connection.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import html
import json
import math
from pathlib import Path

import folium
from branca.element import MacroElement
from jinja2 import Template
import osmnx as ox

PROFILES = {
    "drive_reference": "Drive 기준망",
    "drive_service": "Drive + Service (기본 표시)",
    "relief_vehicle_candidate": "구호차량 후보망 (통행 현장검증 전)",
}
OUTPUT_NAME = "integrated_road_network_20261120.html"
QA_NAME = "facilities_connected_access_147.csv"
COLORS = {"ordinary": "#2775ad", "service": "#ed8325"}
WARNING_COLOR = "#df3b45"
VECTOR_STYLE = "https://tiles.openfreemap.org/styles/liberty"
VECTOR_PROVIDER = "OpenFreeMap"


def digest(path: Path) -> str:
    h = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def checked_file(folder: Path, relative: str, expected: dict) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"Unsafe input path: {relative}")
    file = (folder / relative).resolve(strict=True)
    if not file.is_relative_to(folder.resolve()):
        raise ValueError(f"Input escapes artifact directory: {relative}")
    if expected.get(relative) != digest(file):
        raise ValueError(f"Artifact hash mismatch: {relative}")
    return file


def load_verified_rows(qa_folder: Path) -> tuple[list[dict], dict]:
    metadata_file = qa_folder / "qa_metadata.json"
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    if metadata.get("status") != "qa_data_validated":
        raise ValueError("Connected-edge QA is not validated")
    file = checked_file(qa_folder, QA_NAME, metadata["output_sha256"])
    with file.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 147:
        raise ValueError(f"Expected 147 original facility rows, got {len(rows)}")
    unique = {(r["시군명"], r["시설일련번호"]) for r in rows}
    if len(unique) != 147:
        raise ValueError("Facility identities are not unique")
    evaluated = [r for r in rows if r["qa_status"] == "evaluated"]
    missing = [r for r in rows if r["qa_status"] == "excluded_missing_coordinates"]
    if len(evaluated) != 146 or len(missing) != 1:
        raise ValueError("Expected 146 evaluated and one excluded facility")
    warnings = 0
    for row in evaluated:
        for name in PROFILES:
            if row[f"{name}_connected_status"] != "measured_within_saved_graph_extent":
                raise ValueError(f"No qualified road for {row['시설명']}/{name}")
            if row[f"{name}_connected_search_covered"] != "True":
                raise ValueError(f"Incomplete search extent for {row['시설명']}/{name}")
            near = float(row[f"{name}_nearest_distance_m"])
            connected = float(row[f"{name}_connected_distance_m"])
            if not all(map(math.isfinite, (near, connected))) or near < 0 or connected < near - 0.00001:
                raise ValueError(f"Invalid proximity for {row['시설명']}/{name}")
            warnings += row[f"{name}_nearest_assignment_review"] == "True"
    if warnings != metadata.get("alerts"):
        raise ValueError("Connected-edge alert count does not match QA metadata")
    return rows, metadata


def highway_name(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(map(str, value))
    return str(value or "unknown")


def geometry_coordinates(graph, u, v, attributes) -> list[list[float]]:
    geometry = attributes.get("geometry")
    if geometry is not None:
        if geometry.geom_type != "LineString":
            raise ValueError("Only verified LineString road edges are supported")
        coords = geometry.coords
    else:
        coords = [(graph.nodes[u]["x"], graph.nodes[u]["y"]),
                  (graph.nodes[v]["x"], graph.nodes[v]["y"])]
    return [[round(float(x), 7), round(float(y), 7)] for x, y in coords]


def road_features(graph) -> dict:
    """Deduplicate exact forward/reverse drawings, preserving source geometry."""
    seen = set()
    features = []
    for u, v, _, attributes in graph.edges(keys=True, data=True):
        coords = geometry_coordinates(graph, u, v, attributes)
        if len(coords) < 2:
            continue
        points = tuple(tuple(p) for p in coords)
        road_name = highway_name(attributes.get("highway"))
        dedup = (min(points, tuple(reversed(points))), road_name)
        if dedup in seen:
            continue
        seen.add(dedup)
        is_service = "service" in road_name.split(", ")
        way_id = attributes.get("osmid", "unknown")
        if isinstance(way_id, (list, tuple)):
            way_id = ", ".join(map(str, way_id))
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "way_id": str(way_id),
                "road_type": road_name,
                "class": "service" if is_service else "ordinary",
            },
        })
    return {"type": "FeatureCollection", "features": features}


def facility_popup(row: dict) -> str:
    e = lambda value: html.escape(str(value), quote=True)
    lines = [
        '<div style="font:13px/1.5 sans-serif;min-width:260px">',
        f'<b>{e(row["시설명"])}</b> · {e(row["시군명"])} / {e(row["시설일련번호"])}',
        f'<div>{e(row["상세주소"])}</div>',
        '<hr style="border:0;border-top:1px solid #ddd">',
        '<table style="font-size:12px;text-align:left">',
        '<tr><th>도로망</th><th>단순 최근접</th><th>왕복 연결 적격</th></tr>',
    ]
    for profile, label in PROFILES.items():
        near = float(row[f"{profile}_nearest_distance_m"])
        connected = float(row[f"{profile}_connected_distance_m"])
        lines.append(f'<tr><td>{e(label)}</td><td>{near:.2f}m</td><td>{connected:.2f}m</td></tr>')
    lines += [
        '</table>',
        '<small>시설 위치→도로 형상 직선거리. 주행거리·실제 진입허가 아님.</small>',
        '</div>',
    ]
    return "".join(lines)



class OpenFreeMapBackground(MacroElement):
    """Display OSM-derived streets/labels using OpenFreeMap vector service.

    Leaflet retains all facility, QA, road geometry and layer controls.
    If MapLibre/WebGL/network is unavailable, the embedded roads still render.
    """
    _template = Template("""
    {% macro script(this, kwargs) %}
    (function () {
      var map = {{ this._parent.get_name() }};
      if (typeof L.maplibreGL !== 'function' || typeof maplibregl === 'undefined') {
        console.warn('MapLibre is unavailable; showing embedded roads without online background.');
        return;
      }
      try {
        var vectorBackground = L.maplibreGL({
          style: 'https://tiles.openfreemap.org/styles/liberty',
          pane: 'tilePane',
          interactive: false
        });
        vectorBackground.addTo(map);
        map.attributionControl.addAttribution(
          '<a href="https://openfreemap.org/" target="_blank">OpenFreeMap</a> | ' +
          '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap contributors</a>'
        );
        var toggle = L.control({position: 'bottomleft'});
        toggle.onAdd = function () {
          var btn = L.DomUtil.create('button', 'leaflet-bar');
          btn.type = 'button';
          btn.textContent = '배경지도 끄기';
          btn.title = 'OpenFreeMap 배경지도 표시/숨기기';
          btn.style.cssText = 'background:#fff;padding:7px 10px;border:1px solid #ccc;' +
                              'border-radius:4px;cursor:pointer;font:12px sans-serif';
          L.DomEvent.disableClickPropagation(btn);
          L.DomEvent.on(btn, 'click', function (event) {
            L.DomEvent.stop(event);
            if (map.hasLayer(vectorBackground)) {
              map.removeLayer(vectorBackground);
              btn.textContent = '배경지도 켜기';
            } else {
              vectorBackground.addTo(map);
              btn.textContent = '배경지도 끄기';
            }
          });
          return btn;
        };
        toggle.addTo(map);
      } catch (error) {
        console.warn('OpenFreeMap could not start; embedded roads remain visible.', error);
      }
    })();
    {% endmacro %}
    """)


def add_openfreemap_background(view):
    """Register MapLibre assets *after Leaflet* in Folium's own JS loading order.

    Adding JavascriptLink directly to Figure.header can put the Leaflet bridge
    before Leaflet itself; the page then renders roads but no vector basemap.
    Folium's add_js_link registers resources after its built-in Leaflet scripts.
    """
    view.add_css_link(
        "openfreemap_maplibre_css",
        "https://cdn.jsdelivr.net/npm/maplibre-gl@5/dist/maplibre-gl.css",
    )
    view.add_js_link(
        "openfreemap_maplibre_js",
        "https://cdn.jsdelivr.net/npm/maplibre-gl@5/dist/maplibre-gl.js",
    )
    view.add_js_link(
        "openfreemap_leaflet_binding",
        "https://cdn.jsdelivr.net/npm/@maplibre/maplibre-gl-leaflet@0.1.4/leaflet-maplibre-gl.js",
    )
    OpenFreeMapBackground().add_to(view)


def add_county_boundaries(view):
    """Embed the existing two county outlines; do not request any map tiles."""
    root = Path(__file__).resolve().parents[1]
    folder = root / "data" / "20261109T045434949410Z"
    for code, label in (("buyeo", "부여군"), ("cheongyang", "청양군")):
        path = folder / code / "boundary.geojson"
        if not path.is_file():
            raise FileNotFoundError(f"Preserved county boundary unavailable: {path}")
        source = json.loads(path.read_text(encoding="utf-8"))
        if source.get("type") not in ("FeatureCollection", "Feature", "Polygon", "MultiPolygon"):
            raise ValueError(f"Unexpected boundary GeoJSON: {path}")
        folium.GeoJson(
            source,
            name=f"{label} 경계 (과거 OSM 행정경계)",
            style_function=lambda _: {
                "color": "#616f7e", "weight": 2,
                "fill": True, "fillColor": "#dbe4ed", "fillOpacity": 0.10,
                "dashArray": "6,5",
            },
        ).add_to(view)


def add_facilities(view, rows):
    markers = folium.FeatureGroup(name="시설 146곳 (빨강: QA 보정)", show=True)
    warned_facilities = 0
    for row in rows:
        if row["qa_status"] != "evaluated":
            continue
        flagged = any(row[f"{p}_nearest_assignment_review"] == "True" for p in PROFILES)
        warned_facilities += flagged
        folium.CircleMarker(
            location=[float(row["latitude"]), float(row["longitude"])],
            radius=6 if flagged else 3.5,
            color=WARNING_COLOR if flagged else "#186b46",
            fill=True, fill_opacity=0.9, weight=1,
            tooltip=f'{row["시군명"]} · {row["시설명"]}' + (" (QA 보정)" if flagged else ""),
            popup=folium.Popup(facility_popup(row), max_width=410),
        ).add_to(markers)
    markers.add_to(view)
    return warned_facilities


def draw(profile_folder: Path, qa_folder: Path, output_folder: Path) -> dict:
    profile_folder = profile_folder.resolve(strict=True)
    qa_folder = qa_folder.resolve(strict=True)
    output_folder = output_folder.resolve()
    if output_folder.exists():
        raise FileExistsError(f"Output folder already exists: {output_folder}")
    if output_folder.is_relative_to(profile_folder) or output_folder.is_relative_to(qa_folder):
        raise ValueError("Viewer output must not be inside input artifacts")
    profile_meta = json.loads((profile_folder / "metadata.json").read_text(encoding="utf-8"))
    if profile_meta.get("status") != "data_validated":
        raise ValueError("Source road profile artifact is not validated")
    rows, qa_meta = load_verified_rows(qa_folder)
    source = profile_meta["source"]
    if source["sha256"] != qa_meta["profile_source"]["sha256"]:
        raise ValueError("QA and road profiles refer to different PBF snapshots")
    if source["sha256"] != "492ceebb946500fa54dd1b460b4ff923884dd9e673f65d3750e1d60122b85a8e":
        raise ValueError("This dated viewer must use the verified 2026-11-20 PBF snapshot")

    # OSM's volunteer raster tile servers block file:// pages without Referer.
    # OpenFreeMap documents public vector styles for Leaflet+MapLibre (no API key).
    view = folium.Map(location=[36.32, 126.83], zoom_start=10,
                      tiles=None, prefer_canvas=True, control_scale=True)
    view.get_root().header.add_child(folium.Element(
        "<style>.leaflet-container{background:#f8fafc!important}</style>"
    ))
    add_openfreemap_background(view)
    add_county_boundaries(view)
    counts = {}
    extent = None
    for name, label in PROFILES.items():
        graph_file = checked_file(profile_folder, f"{name}/simplified.graphml", profile_meta["output_sha256"])
        graph = ox.load_graphml(graph_file)
        if not graph.is_directed() or not graph.is_multigraph() or graph.number_of_edges() == 0:
            raise ValueError(f"Invalid road graph: {name}")
        nodes_x = [float(attrs["x"]) for _, attrs in graph.nodes(data=True)]
        nodes_y = [float(attrs["y"]) for _, attrs in graph.nodes(data=True)]
        bbox = (min(nodes_y), min(nodes_x), max(nodes_y), max(nodes_x))
        if extent is None:
            extent = bbox
        else:
            extent = (min(extent[0], bbox[0]), min(extent[1], bbox[1]),
                      max(extent[2], bbox[2]), max(extent[3], bbox[3]))
        feature_data = road_features(graph)
        counts[name] = {
            "graph_nodes": graph.number_of_nodes(),
            "directed_edges": graph.number_of_edges(),
            "visible_physical_lines": len(feature_data["features"]),
        }
        layer = folium.FeatureGroup(name=label, show=name == "drive_service")
        folium.GeoJson(
            feature_data,
            name=label,
            style_function=lambda feature: {
                "color": COLORS[feature["properties"]["class"]],
                "weight": 2 if feature["properties"]["class"] == "service" else 1.6,
                "opacity": 0.85,
            },
            smooth_factor=0.6,
            tooltip=folium.GeoJsonTooltip(fields=["way_id", "road_type"],
                                          aliases=["OSM Way:", "도로 분류:"]),
        ).add_to(layer)
        layer.add_to(view)
        del graph, feature_data

    warned = add_facilities(view, rows)
    view.fit_bounds([[extent[0], extent[1]], [extent[2], extent[3]]])
    folium.LayerControl(collapsed=False, position="topright").add_to(view)
    title = (
        '<div style="position:fixed;z-index:9999;top:12px;left:60px;'
        'width:380px;max-width:calc(100vw - 110px);padding:10px 14px;'
        'background:white;border:1px solid #ddd;border-radius:6px;'
        'box-shadow:0 1px 7px #999;font:13px/1.5 sans-serif">'
        '<b>부여군·청양군 통합 도로망 · 2026-11-20 PBF</b>'
        '<div>파랑: 일반도로 · 주황: Service · 초록: 시설 · 빨강: QA 보정 시설</div>'
        f'<div>시설 146/147 · QA 보정 시설 {warned}곳 · 기본 레이어 Drive+Service</div>'
        '<small>도로 형상·OSM 연결성만 검증. 실제 구호차량 통행·주행거리는 미확정.</small>'
        '<div><small>실제 배경: OpenFreeMap (온라인 벡터지도). 외부 OpenStreetMap 래스터 타일은 사용하지 않음.</small></div>'
        '</div>'
    )
    view.get_root().html.add_child(folium.Element(title))
    output_folder.mkdir(parents=True)
    html_file = output_folder / OUTPUT_NAME
    view.save(str(html_file))
    if html_file.stat().st_size < 10000:
        raise ValueError("Generated HTML is unexpectedly small")
    summary = {
        "status": "viewer_generated_from_validated_artifacts",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "html_file": OUTPUT_NAME,
        "html_sha256": digest(html_file),
        "html_bytes": html_file.stat().st_size,
        "pbf_sha256": source["sha256"],
        "source_profile_status": profile_meta["status"],
        "source_qa_status": qa_meta["status"],
        "original_facilities": 147,
        "plotted_facilities": 146,
        "coordinate_missing": 1,
        "per_profile_alerts": qa_meta["alerts"],
        "distinct_alert_facilities": warned,
        "road_layers": counts,
        "national_pbf_opened": False,
        "routing_performed": False,
        "actual_vehicle_access_certified": False,
        "raster_tile_requests": 0,
        "background": "openfreemap_vector_liberty_with_embedded_county_fallback",
        "vector_tile_style": VECTOR_STYLE,
        "vector_tile_provider": VECTOR_PROVIDER,
        "requires_internet_for_vector_basemap": True,
        "requires_internet_for_leaflet_javascript_css": True,
    }
    (output_folder / "viewer_metadata.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-artifact", type=Path, required=True)
    parser.add_argument("--qa-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = draw(args.profile_artifact, args.qa_artifact, args.output)
    print(json.dumps({
        "status": summary["status"],
        "html_bytes": summary["html_bytes"],
        "plotted_facilities": summary["plotted_facilities"],
        "distinct_alert_facilities": summary["distinct_alert_facilities"],
        "road_layers": summary["road_layers"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
