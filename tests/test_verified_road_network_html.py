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
    page = view.get_root().render()
    assert "부여군" in page
    assert "청양군" in page
    assert "tile.openstreetmap.org" not in page
    assert "L.tileLayer(" not in page
