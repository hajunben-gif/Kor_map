"""Create a visual SEG_CODE verification kit from four 100-year flood SHPs.

Does NOT infer code => depth. Inspect official PNG and selectable code geometry
side by side, then record confirmed mappings from evidence.
Example:
  python -m scripts.verify_flood_seg_code --input-dir flood_100y_raw \
    --source-zip flood_100y_sources.zip --output-dir flood_seg_code_review
"""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import hashlib
import html
import json
from pathlib import Path
import re
import zipfile

import folium
import geopandas as gpd
from branca.element import MacroElement, Template
from shapely.ops import unary_union

CODES = tuple(f"N33{x}" for x in range(5))
COUNTIES = {"44760": "부여군", "44790": "청양군"}
RIVERS = {"NTN": "국가하천", "RGN": "지방하천"}
# These colors are for SHP code identity only, NOT depth/safety interpretation.
CODE_COLORS = {"N330": "#e53935", "N331": "#fb8c00",
               "N332": "#3d8c43", "N333": "#1565c0", "N334": "#7941ab"}
OFFICIAL_DEPTH_LABELS = ("0.5m 이하", "0.5~1.0m", "1.0~2.0m",
                         "2.0~5.0m", "5.0m 이상")
IMAGE_NAME = re.compile(
    r"^RFM_SGG_(NTN|RGN)_(44760|44790)_100(?:\(\d+\))?\.png$",
    re.IGNORECASE,
)


class OpenFreeMapBackground(MacroElement):
    """Load vector basemap after Leaflet, not before (avoids bridge load race)."""
    _template = Template("""
    {% macro script(this, kwargs) %}
    (function(){
      var map={{this._parent.get_name()}};
      if(typeof L.maplibreGL === "function" && typeof maplibregl !== "undefined") {
        try {
          L.maplibreGL({
            style:"https://tiles.openfreemap.org/styles/liberty",
            pane:"tilePane", interactive:false
          }).addTo(map);
        } catch (e) { console.warn("Optional OpenFreeMap unavailable:",e); }
      }
    })();
    {% endmacro %}
    """)


def add_map_background(view):
    view.add_css_link("maplibre_css", "https://cdn.jsdelivr.net/npm/maplibre-gl@5/dist/maplibre-gl.css")
    view.add_js_link("maplibre_js", "https://cdn.jsdelivr.net/npm/maplibre-gl@5/dist/maplibre-gl.js")
    view.add_js_link("leaflet_maplibre", "https://cdn.jsdelivr.net/npm/@maplibre/maplibre-gl-leaflet@0.1.4/leaflet-maplibre-gl.js")
    OpenFreeMapBackground().add_to(view)


def get_reference_pngs(source_zip):
    """Read official previews from provided ZIP, never georeference them."""
    if source_zip is None:
        return {}
    result = {}
    with zipfile.ZipFile(source_zip) as archive:
        for info in archive.infolist():
            name = Path(info.filename.replace("\\", "/")).name
            m = IMAGE_NAME.fullmatch(name)
            if not m:
                continue
            key = f"{m.group(1).upper()}_{m.group(2)}"
            if key in result:
                continue
            if info.file_size > 25_000_000:
                raise ValueError(f"Image too large to embed: {name}")
            b = archive.read(info)
            if not b.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError(f"Invalid PNG magic: {name}")
            result[key] = b
    return result


def create(input_dir: Path, output_dir: Path, source_zip: Path | None = None):
    input_dir = input_dir.resolve(strict=True)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"Not overwriting previous review folder: {output_dir}")
    if source_zip:
        source_zip = source_zip.resolve(strict=True)
    images = get_reference_pngs(source_zip)
    layers = {}
    centers = {}
    for county, county_name in COUNTIES.items():
        for river, river_name in RIVERS.items():
            stem = f"RFM_SGG_{river}_{county}_100"
            shp = input_dir / f"{stem}.shp"
            if not shp.exists():
                raise FileNotFoundError(shp)
            frame = gpd.read_file(shp)
            if frame.crs is None or frame.crs.to_epsg() != 5186:
                raise ValueError(f"Unexpected CRS for {stem}: {frame.crs}")
            if not {"SEG_CODE", "FLDLV_FREQ", "SGG_CD"}.issubset(frame.columns):
                raise ValueError(f"Missing attributes in {stem}")
            if len(frame) != 5 or set(frame.SEG_CODE) != set(CODES):
                raise ValueError(f"Unexpected depth code list in {stem}")
            if set(frame.FLDLV_FREQ.astype(str).str.strip()) != {"100"}:
                raise ValueError(f"Frequency differs from 100 in {stem}")
            if set(frame.SGG_CD.astype(str).str.strip()) != {county}:
                raise ValueError(f"County code mismatch in {stem}")
            if frame.geometry.is_empty.any() or (~frame.geometry.is_valid).any():
                raise ValueError(f"Invalid original geometry in {stem}")
            frame = frame.to_crs(epsg=4326)
            name = f"{county_name} / {river_name}"
            centers[name] = []
            record = {}
            for _, row in frame.iterrows():
                code = row.SEG_CODE
                geom = row.geometry.simplify(0.00006, preserve_topology=True)
                if not geom.is_valid:
                    raise ValueError(f"Invalid simplified geometry: {stem} {code}")
                p = row.geometry.representative_point()
                centers[name].append({
                    "code": code, "lat": round(p.y, 7), "lon": round(p.x, 7),
                    "color": CODE_COLORS[code],
                })
                record[code] = geom
            layers[name] = record

    output_dir.mkdir(parents=True)
    # A single map with four selectable source groups; all code colors are *arbitrary*.
    m = folium.Map(location=[36.35, 126.83], zoom_start=10,
                   tiles=None, prefer_canvas=True, control_scale=True)
    add_map_background(m)
    for source_name, features in layers.items():
        group = folium.FeatureGroup(name=source_name, show=("부여군" in source_name and "국가하천" in source_name))
        for code, geom in sorted(features.items()):
            folium.GeoJson(
                geom.__geo_interface__,
                name=code,
                style_function=lambda _f, c=code: {
                    "color": CODE_COLORS[c], "weight": 1.1,
                    "fillColor": CODE_COLORS[c], "fillOpacity": .55
                },
                highlight_function=lambda _f: {"weight": 2.8, "fillOpacity": .85},
                tooltip=folium.Tooltip(f"{html.escape(source_name)} — {code} (수심 미확정)", sticky=True),
            ).add_to(group)
        group.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(output_dir / "code_overlay_map.html"))

    official_png = {}
    for river in RIVERS:
        for county in COUNTIES:
            key = f"{river}_{county}"
            if key in images:
                official_png[key] = "data:image/png;base64," + base64.b64encode(images[key]).decode("ascii")
    # Official images are static, not GIS rasters; reviewer independently compares the same locations.
    options = "".join(
        '<option value="' + html.escape(key) + '">' +
        html.escape(f"{COUNTIES[key.split('_')[1]]} / {RIVERS[key.split('_')[0]]}") +
        "</option>"
        for key in official_png
    )
    imgdata = json.dumps(official_png, ensure_ascii=False)
    legend = "".join(
        f'<span class="code"><i style="background:{CODE_COLORS[code]}"></i>{code} (수심 미확정)</span>'
        for code in CODES
    )
    review = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>침수심 코드 대조 — 공식 PNG vs GIS</title>
<style>
body{margin:0;font:14px/1.4 system-ui,sans-serif;color:#1c2936;background:#f7f9fc}
header{padding:12px 20px;background:#fff;border-bottom:1px solid #ddd}
h2{margin:0 0 6px;font-size:18px}
p{margin:5px 0}.alert{color:#a32829;font-weight:650}
.panel{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:10px;height:calc(100vh - 215px);min-height:350px}
.panel>section{display:flex;flex-direction:column;min-width:0;border:1px solid #ddd;background:#fff;border-radius:8px;overflow:hidden}
.panel h3{margin:0;padding:8px 12px;border-bottom:1px solid #ddd;font-size:14px}
iframe{border:0;flex:1;min-height:250px}.imgbox{flex:1;overflow:auto;min-height:250px;display:grid;place-items:center;background:#182a36}
.imgbox img{max-width:100%;height:auto;object-fit:contain;display:block}
footer{padding:8px 16px;background:#fff;border-top:1px solid #ddd}
.code{display:inline-flex;gap:5px;align-items:center;margin-right:14px;white-space:nowrap}
.code i{width:12px;height:12px;border-radius:2px}
@media(max-width:780px){.panel{grid-template-columns:1fr;height:auto}.panel>section{height:430px}}
</style></head><body>
<header><h2>100년 빈도 범람지도: 코드 ↔ 색상 대조</h2>
<p class="alert">왼쪽 코드색은 임시 구분색이며 침수심을 뜻하지 않습니다. 오른쪽 공식 PNG의 색·범례와 같은 위치를 대조하세요.</p>
<label>공식 이미지 선택: <select id="source">__OPTIONS__</select></label>
<p>공식 범례: 0.5m 이하 · 0.5~1m · 1~2m · 2~5m · 5m 이상</p>
</header><main class="panel"><section><h3>GIS SHP 코드 레이어 (켜고 끌 수 있음)</h3>
<iframe src="code_overlay_map.html" title="코드별 공간지도"></iframe></section>
<section><h3>공식 범람지도 이미지 (좌표없는 PNG)</h3>
<div class="imgbox"><img id="official" alt="공식 침수심 색상 범람지도"></div></section></main>
<footer>__LEGEND__<p>각 코드의 대표 좌표는 representative_points.csv에 정리되어 있습니다.
각 코드가 어떤 수심인지 확인되기 전까지 도로 차단 시나리오에 연결하지 마세요.</p></footer>
<script>const imgs=__DATA__;const sel=document.getElementById('source');
function show(){document.getElementById('official').src=imgs[sel.value]||"";}
sel.addEventListener('change',show);show();</script></body></html>"""
    review = review.replace("__OPTIONS__", options).replace("__LEGEND__", legend).replace("__DATA__", imgdata)
    (output_dir / "code_comparison.html").write_text(review, encoding="utf-8")

    import csv
    with (output_dir / "representative_points.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["source", "code", "lat", "lon", "color"])
        writer.writeheader()
        for name, points in centers.items():
            for item in points:
                writer.writerow({"source": name, **item})
    manifest = {
        "status": "visual_comparison_only_not_verified_codebook",
        "code_colors_are_arbitrary": True,
        "code_to_depth_mapping": None,
        "image_keys_found": list(official_png),
        "datasets": list(layers),
        "total_code_features": sum(map(len, layers.values())),
        "source_zip_pngs_not_georeferenced": True,
        "note": "Compare 5 code polygons to official PNG legend manually; do not infer from centrality.",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "code_features": manifest["total_code_features"],
                      "official_reference_images": len(official_png)}, ensure_ascii=False))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--source-zip", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    create(args.input_dir, args.output_dir, args.source_zip)


if __name__ == "__main__":
    main()
