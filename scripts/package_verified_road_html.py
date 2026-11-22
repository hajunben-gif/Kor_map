"""Package a verified interactive road-network HTML as a durable GitHub ZIP.

Does not modify road data, download OSM, recompute QA, or fetch map tiles.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import zipfile

HTML = "integrated_road_network_20261120.html"
MANIFEST = "viewer_metadata.json"
PBF_SHA256 = "492ceebb946500fa54dd1b460b4ff923884dd9e673f65d3750e1d60122b85a8e"
README = """부여군·청양군 검증 통합 도로망 · 2026-11-20 PBF

1. integrated_road_network_20261120.html을 Chrome/Edge로 여세요.
2. 인터넷 연결이 필요합니다(OpenFreeMap 배경, Leaflet/MapLibre JS·CSS).
3. 오른쪽 위에서 Drive / Drive+Service / 구호차량 후보망을 선택하세요.
4. 초록색은 시설, 빨간색은 QA 보정 시설입니다. 시설을 클릭하면 최근접 거리와 연결 적격 거리가 표시됩니다.
5. 파란 선은 일반도로, 주황 선은 Service 도로이며 실제 구호차량 통행 가능 여부는 미확인입니다.

원자료 및 코드: https://github.com/hajunben-gif/Kor_map
분석 출처: OpenStreetMap contributors / ODbL 1.0
배경 지도: OpenFreeMap / OpenStreetMap contributors
이 자료는 침수·실제 주행거리·회전 제한·현장 출입구를 검증한 결과가 아닙니다.
"""


def sha_file(file: Path) -> str:
    digest = sha256()
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    html_path = (source / HTML).resolve(strict=True)
    manifest_path = (source / MANIFEST).resolve(strict=True)
    if html_path.parent != source or manifest_path.parent != source:
        raise ValueError("Source files must be directly within the selected artifact")
    metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        "status": "viewer_generated_from_validated_artifacts",
        "html_file": HTML,
        "source_profile_status": "data_validated",
        "source_qa_status": "qa_data_validated",
        "pbf_sha256": PBF_SHA256,
        "original_facilities": 147,
        "plotted_facilities": 146,
        "coordinate_missing": 1,
        "per_profile_alerts": 3,
        "distinct_alert_facilities": 2,
        "raster_tile_requests": 0,
        "background": "openfreemap_vector_liberty_with_embedded_county_fallback",
        "vector_tile_style": "https://tiles.openfreemap.org/styles/liberty",
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise ValueError(f"Unexpected verified viewer metadata {key}: {metadata.get(key)!r}")
    if set(metadata.get("road_layers", {})) != {
        "drive_reference", "drive_service", "relief_vehicle_candidate"
    }:
        raise ValueError("Missing one or more verified road profiles")
    if sha_file(html_path) != metadata.get("html_sha256"):
        raise ValueError("Verified HTML SHA-256 mismatch")
    if html_path.stat().st_size != metadata.get("html_bytes"):
        raise ValueError("Verified HTML byte count mismatch")
    # Source hash proves the file is the successful, fully rendered 2026-11-22
    # viewer; the packaging step does not change its HTML bytes.
    page = html_path.read_text(encoding="utf-8")
    if "https://tiles.openfreemap.org/styles/liberty" not in page:
        raise ValueError("Expected functional OpenFreeMap background style missing")
    if "tile.openstreetmap.org" in page:
        raise ValueError("Blocked OSM raster-tile endpoint is present")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing ZIP: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        archive.write(html_path, arcname=HTML)
        archive.write(manifest_path, arcname=MANIFEST)
        archive.writestr("README_열기.txt", README)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Packaged ZIP integrity check failed")
        if archive.namelist() != [HTML, MANIFEST, "README_열기.txt"]:
            raise ValueError("Unexpected ZIP contents")
    result_sha = sha_file(destination)
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{result_sha}  {destination.name}\n", encoding="ascii"
    )
    print(json.dumps({
        "result": "verified-html-zip-created",
        "path": str(destination),
        "zip_bytes": destination.stat().st_size,
        "zip_sha256": result_sha,
        "html_bytes": metadata["html_bytes"],
        "plotted_facilities": 146,
        "map_background": metadata["vector_tile_style"],
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    args = parser.parse_args()
    package(args.source, args.zip)


if __name__ == "__main__":
    main()
