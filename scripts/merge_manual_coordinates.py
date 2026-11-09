"""Merge four user-supplied coordinates into the real 142-success Actions CSV.

Does not call any geocoding API or modify the OSM road network.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import shutil
import zipfile

import folium
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
MANUAL = {
    '왕진1리회관': ('36.329275', '126.939026'),
    '서정1리회관': ('36.412012', '126.946154'),
    '용두리회관': ('36.424176', '126.933180'),
    '관생리회관': ('36.407545', '126.957853'),
}
NOTE = '사용자 제공 Google Maps 수동 확인 좌표. 별도 현장 검증을 거치지 않음. 시설 출입구·차량 진입점 미확인.'
EXCLUSION = '좌표를 확인하지 못해 위치 미확인. 도로 접근성 계산에서 제외.'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source, out, networks):
    original = pd.read_csv(source, dtype=str, keep_default_na=False)
    frame = original.copy(deep=True)
    assert len(frame) == 147
    assert frame['시군명'].str.strip().value_counts().to_dict() == {'청양군': 117, '부여군': 30}
    success = original['geocoding_status'].eq('success')
    assert int(success.sum()) == 142, '실제 성공 142행인 Actions 결과가 필요합니다.'
    assert original.loc[success, ['latitude', 'longitude']].ne('').all().all()
    assert set(original.loc[~success, '시설명']) == set(MANUAL) | {'대동마을회관'}
    assert original.loc[~success, ['latitude', 'longitude']].eq('').all().all()
    assert not frame[['시군명', '시설일련번호']].duplicated().any()

    files = sorted(p for p in networks.rglob('*') if p.suffix in ('.graphml', '.gpkg', '.geojson'))
    before = {str(p.relative_to(ROOT)): sha(p) for p in files}
    boundaries = {}
    for slug in ('buyeo', 'cheongyang'):
        b = gpd.read_file(networks / slug / 'boundary.geojson')
        assert b.crs is not None
        boundaries[slug] = b.to_crs(4326).geometry.union_all()
    county = boundaries['cheongyang']
    assert county.is_valid
    now = datetime.now(timezone.utc).isoformat()
    for col in ('manual_validation_note', 'coordinate_recorded_at_utc',
                'accessibility_exclusion_reason', 'original_geocoding_status',
                'original_geocoding_source', 'original_geocoding_error_code',
                'original_geocoding_error_message'):
        frame[col] = ''
    frame['field_verification_status'] = 'not_field_verified'
    frame['road_accessibility_eligible'] = 'true'
    audit = []
    for name, (lat, lon) in MANUAL.items():
        rows = frame.index[frame['시설명'].eq(name)]
        assert len(rows) == 1, f'{name}: 시설명 중복 또는 누락'
        i = rows[0]
        assert frame.at[i, '시군명'].strip() == '청양군'
        assert '청양군' in frame.at[i, '상세주소']
        assert not success.at[i], f'{name}: 기존 성공 좌표 덮어쓰기 금지'
        assert county.covers(Point(float(lon), float(lat))), f'{name}: 청양군 경계 밖'
        for col in ('geocoding_status', 'geocoding_source', 'geocoding_error_code', 'geocoding_error_message'):
            frame.at[i, 'original_' + col] = original.at[i, col]
        frame.at[i, 'latitude'] = lat
        frame.at[i, 'longitude'] = lon
        frame.at[i, 'geocoding_status'] = 'success_manual'
        frame.at[i, 'geocoding_source'] = 'Google Maps (manual)'
        frame.at[i, 'boundary_validation'] = 'inside_county'
        frame.at[i, 'geocoding_error_code'] = ''
        frame.at[i, 'geocoding_error_message'] = ''
        frame.at[i, 'manual_validation_note'] = NOTE
        frame.at[i, 'coordinate_recorded_at_utc'] = now
        audit.append({'시설명': name, '시군명': '청양군',
            '시설일련번호': frame.at[i, '시설일련번호'], '상세주소': frame.at[i, '상세주소'],
            'latitude': lat, 'longitude': lon, 'source': 'Google Maps (manual)',
            'boundary_validation': 'inside_county', 'note': NOTE})
    rows = frame.index[frame['시설명'].eq('대동마을회관')]
    assert len(rows) == 1
    i = rows[0]
    assert frame.at[i, '시군명'].strip() == '청양군' and not success.at[i]
    for col in ('geocoding_status', 'geocoding_source', 'geocoding_error_code', 'geocoding_error_message'):
        frame.at[i, 'original_' + col] = original.at[i, col]
    frame.loc[i, ['latitude', 'longitude', 'geocoding_source']] = ''
    frame.at[i, 'geocoding_status'] = 'location_unverified'
    frame.at[i, 'geocoding_error_code'] = 'LOCATION_UNVERIFIED'
    frame.at[i, 'geocoding_error_message'] = EXCLUSION
    frame.at[i, 'road_accessibility_eligible'] = 'false'
    frame.at[i, 'accessibility_exclusion_reason'] = EXCLUSION

    # Compare every original column of all 142 successful rows, including coordinate strings.
    assert frame.loc[success, original.columns].equals(original.loc[success])
    located = frame['latitude'].ne('') & frame['longitude'].ne('')
    assert int(located.sum()) == 146
    assert frame.geocoding_status.value_counts().to_dict() == {
        'success': 142, 'success_manual': 4, 'location_unverified': 1}
    out.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source, out / 'kakao_actions_original.csv')
    frame.to_csv(out / 'shelters_final_147.csv', index=False, encoding='utf-8-sig')
    frame.loc[~located].to_csv(out / 'accessibility_excluded.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(audit).to_csv(out / 'manual_coordinate_audit.csv', index=False, encoding='utf-8-sig')

    map_ = folium.Map(location=[36.30, 126.87], zoom_start=10, tiles='OpenStreetMap', prefer_canvas=True)
    edge_counts = {}
    for slug, label in [('buyeo', '부여군'), ('cheongyang', '청양군')]:
        edges = gpd.read_file(networks / slug / 'network.gpkg', layer='edges').to_crs(4326)
        edge_counts[label] = len(edges)
        # Embed every original edge geometry; no network download, alteration, or snapping.
        folium.GeoJson(json.loads(edges[['geometry']].to_json()), name=f'{label} OSMnx 자동차 도로망',
            style_function=lambda _: {'color': '#546e7a', 'weight': 2, 'opacity': .55},
            smooth_factor=0).add_to(map_)
        folium.GeoJson(boundaries[slug].__geo_interface__, name=f'{label} OSM 행정경계',
            style_function=lambda _: {'color': '#455a64', 'weight': 2, 'fillOpacity': .015}).add_to(map_)
    kakao_layer = folium.FeatureGroup(name='Kakao API 좌표 142곳').add_to(map_)
    manual_layer = folium.FeatureGroup(name='Google Maps 수동 좌표 4곳 · 현장 미검증').add_to(map_)
    marker_count = 0
    for _, row in frame.loc[located].iterrows():
        manual = row['geocoding_status'] == 'success_manual'
        note = NOTE if manual else 'Kakao 주소 대표점. 시설 출입구·차량 진입점 및 현장 검증 미확인.'
        fields = [row['시설명'], row['시군명'], row['상세주소'],
                  f"위도 {row['latitude']} / 경도 {row['longitude']}",
                  f"출처: {row['geocoding_source']}", note,
                  f"최대 수용가능인원: {row['주거능력']} (실제 이재민 수 아님)"]
        folium.CircleMarker([float(row['latitude']), float(row['longitude'])],
            radius=7 if manual else 4, color='#f57c00' if manual else '#1565c0',
            fill=True, fill_opacity=.9, tooltip=html.escape(row['시설명']),
            popup=folium.Popup('<br>'.join(html.escape(str(v)) for v in fields), max_width=460)
            ).add_to(manual_layer if manual else kakao_layer)
        marker_count += 1
    assert marker_count == 146
    note = '''<div style="position:fixed;bottom:20px;left:15px;z-index:9999;background:white;padding:12px;border:1px solid #777;max-width:430px">
    <b>최종 147곳: 실제 좌표 146곳 · 위치 미확인 1곳</b><br>
    파랑: Kakao API 142곳 / 주황: Google Maps (manual) 4곳<br>
    수동 좌표는 현장 미검증. 출입구·차량 진입점 확인과 별개입니다.<br>
    대동마을회관: 위치 미확인, 마커 및 도로 접근성 계산에서 제외.<br>
    회색: 기존 OSMnx 자동차 도로망 (OpenStreetMap / ODbL).<br>
    지도는 도로와 시설 위치를 함께 표시하며 경로·접근성 점수는 계산하지 않았습니다.
    </div>'''
    map_.get_root().html.add_child(folium.Element(note))
    folium.LayerControl(collapsed=False).add_to(map_)
    combined = boundaries['buyeo'].union(boundaries['cheongyang']).bounds
    map_.fit_bounds([[combined[1], combined[0]], [combined[3], combined[2]]])
    map_.save(str(out / 'shelters_roads_integrated.html'))
    after = {str(p.relative_to(ROOT)): sha(p) for p in files}
    assert before == after, '원본 도로망·경계 파일 변경 감지'
    summary = {'total': 147, 'kakao_api_coordinates_preserved': 142,
        'manual_coordinates': 4, 'located': 146, 'location_unverified': 1,
        'map_markers': marker_count, 'source_run_id': 42587230808, 'source_run_attempt': 2,
        'source_artifact_id': 13659149237, 'source_csv_sha256': sha(source),
        'source_142_rows_all_original_columns_unchanged': True,
        'manual_boundary_validation': '4/4 inside existing OSM Cheongyang administrative boundary',
        'field_verification': 'not performed; manual coordinates supplied by user',
        'crs': 'EPSG:4326', 'csv_encoding': 'UTF-8-SIG', 'merged_at_utc': now,
        'accessibility_excluded': [{'시설명': '대동마을회관', 'reason': EXCLUSION}],
        'accessibility_calculation': 'not performed; eligibility flag only indicates coordinate availability',
        'network_edge_counts': edge_counts, 'network_files_unchanged': True,
        'network_file_sha256_before_and_after': before}
    (out / 'merge_validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'README.md').write_text('''# 최종 시설 좌표 병합 결과

**147행 중 실제 좌표 146곳, 위치 미확인 1곳입니다.**

- Kakao API 성공 좌표 142개와 해당 행의 기존 모든 열은 원본과 동일합니다.
- 사용자 제공 Google Maps (manual) 좌표 4개만 병합했습니다. 네 시설의 이름은 각각 유일하며 모두 청양군 소속, 기존 OSM 청양군 행정경계 내부입니다.
- 수동 좌표는 별도 현장 검증을 거치지 않았습니다. 주소 대표점은 차량 진입점 확인을 의미하지 않습니다.
- 대동마을회관은 좌표 공란, location_unverified, road_accessibility_eligible=false입니다. 제외 사유를 CSV에 기록했습니다.
- 통합 지도에 146개 마커와 두 군의 기존 OSMnx 자동차 도로망 및 행정경계를 포함했습니다. 파랑은 Kakao, 주황은 Google Maps 수동 좌표입니다.
- 도로 접근성 점수·최단 경로·도로 스냅은 계산하지 않았습니다. 접근성 대상 플래그는 좌표 유무에 대한 사전 필터입니다.
- 원본 도로망·경계 파일은 변경하지 않았으며 SHA-256 전후 비교로 확인했습니다.
- 지도 HTML에 시설·도로 데이터가 포함되어 있습니다. 배경 타일 및 Leaflet 라이브러리는 인터넷이 필요합니다.

## 파일

`shelters_final_147.csv`: 최종 147행 (UTF-8-SIG). `latitude`=위도(y), `longitude`=경도(x), WGS84.

`shelters_roads_integrated.html`: 시설 146곳 및 기존 도로망 통합 지도.

`kakao_actions_original.csv`: 실제 Actions artifact의 원본 CSV (변경 없음).

`manual_coordinate_audit.csv`: 네 수동 좌표의 시설 식별·주소·경계·출처·현장 미검증 기록.

`accessibility_excluded.csv`: 위치 미확인으로 접근성 계산에서 제외하는 1곳.

`merge_validation.json`: 수량, 출처, 기존 성공 행 불변 검증 및 파일 해시.

## 출처

[실제 GitHub Actions 실행 42587230808, attempt 2](https://github.com/hajunben-gif/Kor_map/actions/runs/42587230808/attempts/2), artifact ID 13659149237.

원본 142행은 geocoding_status=success를 유지합니다. 네 수동 좌표는 success_manual, 출처는 Google Maps (manual)입니다. 기존 API 오류 기록은 original_geocoding_* 열에 보존했습니다. manual_validation_note와 field_verification_status에 현장 미검증을 기록했습니다. geocoding_checked_at_utc는 원래 API 조회 시각을 유지하고 수동 병합 시각은 coordinate_recorded_at_utc로 구분했습니다.
''', encoding='utf-8')
    zip_path = out.parent / 'shelters_manual_merged.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.iterdir()):
            z.write(p, p.name)
    print(json.dumps({k: summary[k] for k in ('total', 'located', 'location_unverified',
          'kakao_api_coordinates_preserved', 'manual_coordinates', 'map_markers', 'network_edge_counts')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'output/manual_merge')
    parser.add_argument('--networks', type=Path, default=ROOT/'data/20261109T045434949410Z')
    args = parser.parse_args()
    build(args.source, args.output, args.networks)
