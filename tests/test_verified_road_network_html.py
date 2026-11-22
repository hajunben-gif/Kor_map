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
