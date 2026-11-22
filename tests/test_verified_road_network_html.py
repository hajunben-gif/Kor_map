"""Regression tests for public OSM tile-server 403 on file:// HTML maps."""
import inspect

import folium

from scripts import render_verified_road_network_html as renderer


def test_default_viewer_does_not_request_osm_raster_tiles():
    source = inspect.getsource(renderer.draw)
    assert "tiles=None" in source
    assert 'tiles="OpenStreetMap"' not in source
    assert "tile.openstreetmap.org" not in source


def test_embedded_county_boundaries_work_without_raster_tiles():
    view = folium.Map(location=[36.32, 126.83], zoom_start=10, tiles=None)
    renderer.add_county_boundaries(view)

    # Folium only embeds optional layer *labels* into its generated JS when
    # LayerControl is present. Inspect the actual GeoJson layers directly.
    layers = [child for child in view._children.values()
              if isinstance(child, folium.GeoJson)]
    assert len(layers) == 2
    assert {layer.layer_name for layer in layers} == {
        "부여군 경계 (과거 OSM 행정경계)",
        "청양군 경계 (과거 OSM 행정경계)",
    }
    assert all(layer.data.get("type") in (
        "FeatureCollection", "Feature", "Polygon", "MultiPolygon"
    ) for layer in layers)
    assert all(layer.data for layer in layers)

    page = view.get_root().render()
    assert "tile.openstreetmap.org" not in page
    assert "L.tileLayer(" not in page


def test_online_openfreemap_vector_basemap_is_available_without_osm_raster():
    view = folium.Map(location=[36.32, 126.83], zoom_start=10, tiles=None)
    renderer.add_openfreemap_background(view)
    renderer.add_county_boundaries(view)
    page = view.get_root().render()
    assert "tiles.openfreemap.org/styles/liberty" in page
    assert "maplibre-gl@5" in page
    assert "leaflet-maplibre-gl.js" in page
    assert "배경지도 끄기" in page
    assert "tile.openstreetmap.org" not in page
    assert "L.tileLayer(" not in page


def test_maplibre_falls_back_without_external_map_service():
    view = folium.Map(location=[36.32, 126.83], zoom_start=10, tiles=None)
    renderer.add_openfreemap_background(view)
    renderer.add_county_boundaries(view)
    page = view.get_root().render()
    assert "OpenFreeMap is unavailable" not in page
    assert "embedded roads remain visible" in page


def test_maplibre_dependency_scripts_load_in_correct_order():
    view = folium.Map(location=[36.32, 126.83], zoom_start=10, tiles=None)
    renderer.add_openfreemap_background(view)
    page = view.get_root().render()
    assert page.index("leaflet.js") < page.index("maplibre-gl@5/dist/maplibre-gl.js")
    assert page.index("maplibre-gl@5/dist/maplibre-gl.js") < page.index(
        "leaflet-maplibre-gl.js"
    )
    assert page.index("leaflet-maplibre-gl.js") < page.index(
        "tiles.openfreemap.org/styles/liberty"
    )
