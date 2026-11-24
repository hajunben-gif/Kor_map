"""Build an offline scenario viewer and two lab-sharing ZIPs from passed archives."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from scripts.facility_access_scenarios import PROFILE
from scripts.finalize_facility_access_points import csv_records
from scripts.flood_road_inputs import file_sha256, require
from scripts.qa_flood_road_scenarios import SCENARIOS
from scripts.team_share_data import FINAL_FILES, load_verified_inputs, read_json
from scripts.team_share_geometry import MASK_SIMPLIFY_M, ROAD_SIMPLIFY_M, load_geometry

ROOT = Path(__file__).resolve().parents[1]
VIEWER = Path(__file__).with_name('team_share_viewer')
GITHUB = 'https://github.com/hajunben-gif/Kor_map'
FLOOD_SOURCE = 'https://www.floodmap.go.kr/natreg/'
OSM_LICENSE = 'https://www.openstreetmap.org/copyright'
ONLINE_BASEMAP = dict(provider='OpenFreeMap', style='https://tiles.openfreemap.org/styles/liberty',
                      terms='https://openfreemap.org/tos/', enabled_on_open=True)
RIGHTS_ITEMS = {
    'flood_display': ('flood', 'START_HERE.html의 침수 도형', '홍수위험지도 변경 및 팀 배포 허락'),
    'flood_gpkg': ('flood', 'flood_100y_depth_scenarios.gpkg', '홍수위험지도 파생 GeoPackage의 재배포 허락'),
    'scenario_gpkg': ('s0', 'road_scenarios.gpkg', '침수 분할을 포함한 원본 시나리오 GeoPackage의 재배포 허락'),
    'trace_s1': ('S1', 'S1 edge trace', '침수 분할이 포함된 trace의 재배포 허락'),
    'trace_s2': ('S2', 'S2 edge trace', '침수 분할이 포함된 trace의 재배포 허락'),
    'source_facilities': ('facilities', '원본 시설 CSV', '원본 시설 자료 및 좌표의 재배포 허락'),
}


def distribution_rights(data, path):
    approvals = {}
    if path:
        data['hashes'].record(path)
        approvals = read_json(path)
        require(isinstance(approvals, dict) and approvals.keys() <= RIGHTS_ITEMS.keys(),
                'Unknown distribution rights asset')
    report = {}
    for key, (input_key, label, required) in RIGHTS_ITEMS.items():
        expected = data['digests'][input_key]
        record = approvals.get(key)
        if record is not None:
            require(isinstance(record, dict) and record.get('sha256') == expected
                    and all(isinstance(record.get(k), str) and record[k].strip()
                            for k in ('basis', 'credit')),
                    f'{key}: supply matching SHA-256, permission basis and credit')
        report[key] = dict(asset=label, sha256=expected, included=record is not None,
                           basis=record['basis'] if record else required + ' 확인 필요',
                           credit=record['credit'] if record else '')
    source = data['manifest']['roads']['source']
    osm = source.get('license') == 'OpenStreetMap contributors / ODbL 1.0'
    report['base_road'] = dict(
        asset='road_network_profiles.gpkg 및 source_node_tags.json', sha256=data['digests']['road'],
        included=osm, basis='ODbL 1.0 · ' + OSM_LICENSE if osm else '원본 도로 아티팩트 라이선스 확인 필요',
        credit='© OpenStreetMap contributors' if osm else '',
    )
    return report


def render_html(output, data, geometry, show_masks):
    types, roads, masks, bounds, offline, _ = geometry
    context = json.dumps(dict(sites=data['sites'], stats=data['stats'], roadTypes=types,
                              bounds=bounds, showMasks=show_masks, basemapStyle=ONLINE_BASEMAP['style']),
                         ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).replace('<', r'\u003c')
    replacements = {
        'DATA': context,
        'LEAFLET_CSS': (VIEWER / 'leaflet.css').read_text(encoding='utf-8'),
        'LEAFLET_JS': (VIEWER / 'leaflet.js').read_text(encoding='utf-8'),
        'VIEWER_CSS': (VIEWER / 'viewer.css').read_text(encoding='utf-8'),
        'VIEWER_JS': (VIEWER / 'viewer.js').read_text(encoding='utf-8'),
        'BASEMAP_JS': (VIEWER / 'basemap.js').read_text(encoding='utf-8'),
        'BASEMAP_OFFLINE': offline,
        **{'ROAD_' + s: roads[s] for s in SCENARIOS},
        **{'MASK_' + s: masks[s] if show_masks else '' for s in ('S1', 'S2')},
    }
    template = (VIEWER / 'index.html').read_text(encoding='utf-8')
    for key, value in replacements.items():
        tag = '@@' + key + '@@'
        require(template.count(tag) == 1, f'Missing HTML template slot {key}')
        template = template.replace(tag, value)
    require(all('@@' + name + '@@' not in template for name in replacements), 'Unfilled map template')
    output.write_text(template, encoding='utf-8')


def plot_results(folder, data):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    folder.mkdir()
    stats = data['stats']
    fig, (sites, roads) = plt.subplots(1, 2, figsize=(9.2, 3.5), layout='constrained')
    opens = [stats[s]['open'] for s in SCENARIOS]
    closes = [stats[s]['blocked'] for s in SCENARIOS]
    review = [stats[s]['review'] for s in SCENARIOS]
    sites.bar(SCENARIOS, opens, label='Open', color='#127349')
    sites.bar(SCENARIOS, closes, bottom=opens, label='Blocked', color='#bd3239')
    if any(review):
        sites.bar(SCENARIOS, review, bottom=[a+b for a,b in zip(opens,closes,strict=True)],
                  label='Review', color='#b16117')
    sites.set(ylabel='Facility access points', title='Fixed access sites')
    sites.legend(frameon=False, loc='lower left')
    roads.bar(SCENARIOS, [stats[s]['physical_blocked_m']/1000 for s in SCENARIOS], color='#bd3239')
    roads.set(ylabel='Physical road length (km)', title='Blocked road length')
    for ax in (sites, roads):
        ax.spines[['top', 'right']].set_visible(False)
    fig.savefig(folder / 'scenario_comparison.png', dpi=160)
    plt.close(fig)

    values = [float(r['road_projection_distance_m']) for r in csv_records(
        data['paths']['automatic'], ('road_projection_distance_m',))]
    require(len(values) == data['projection']['facilities'] and all(math.isfinite(v) and v >= 0 for v in values)
            and math.isclose(statistics.mean(values), data['projection']['projection_distance_m']['mean'],
                             abs_tol=1e-6, rel_tol=0), 'Projection plot input differs from verified summary')
    fig, ax = plt.subplots(figsize=(7.2, 3.7), layout='constrained')
    ax.hist(values, bins='auto', color='#28668b', edgecolor='white')
    ax.set(xlabel='Initial straight connection distance (m)', ylabel='Facilities',
           title='Automatic road projection distances')
    for key, color, label in [('upper_fence_m','#a46d22','Upper outlier threshold'),
                               ('extreme_fence_m','#9d334a','Extreme outlier threshold')]:
        ax.axvline(data['projection'][key], color=color, linestyle='--', label=label)
    ax.legend(frameon=False)
    ax.spines[['top', 'right']].set_visible(False)
    fig.savefig(folder / 'projection_distances.png', dpi=160)
    plt.close(fig)


def result_table(stats):
    rows = ['| 시나리오 | 전체 | 개방 | 차단 | 검토 | 차단 도로 km | 침수 면적 km² | 최대 SCC 원본 노드 | 대표점 침수 | 직선 접속선 교차 |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name in SCENARIOS:
        s = stats[name]
        rows.append(f"| {name} | {s['facilities']} | {s['open']} | {s['blocked']} | {s['review']} | "
                    f"{s['physical_blocked_m']/1000:.3f} | {s['flood_km2']:.3f} | "
                    f"{s['largest_scc_original_nodes']} | {s['facility_inside']} | {s['connector_crossings']} |")
    return '\n'.join(rows)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def write_documents(light, data, display, rights):
    sha = data['digests']
    source = data['manifest']['roads']['source']
    omitted = [r['asset'] for r in rights.values() if not r['included']]
    excluded = '\n'.join(f'- {r["asset"]} · {r["basis"]}' for r in rights.values() if not r['included'])
    credits = '\n'.join(f'- {r["asset"]} · {r["credit"]} · {r["basis"]}'
                        for r in rights.values() if r['included'])
    readme = f"""# 부여·청양 침수 도로망 팀 공유 자료

ZIP을 풀고 `START_HERE.html`을 최신 Edge 또는 Chrome에서 더블클릭합니다. OpenFreeMap 일반 배경지도가 기본으로 켜집니다. 연결이나 WebGL에 문제가 생기면 내장 군 경계·지명·주요 도로를 표시합니다. **다시 연결** 버튼으로 재시도할 수 있습니다. 저장된 분석 자료는 인터넷 연결 없이 볼 수 있으며, 온라인 타일은 ZIP에 넣지 않습니다. [OpenFreeMap 이용 조건](https://openfreemap.org/tos/)

## 파일

- `data/scenario_comparison.csv`에는 세 도로 프로파일의 시나리오별 기존 집계가 있습니다.
- `data/`의 시설 CSV 4개에는 고정 접속점, 시나리오 상태, 직선 겹침 및 방향별 부분 간선 연결 자료가 있습니다.
- `charts/`에는 시나리오 비교와 기존 투영 거리 분포 그림이 있습니다.
- `METHODS_KO.md`에는 연구 방법과 검증 결과가 있습니다.
- `SHARING.md`에는 출처와 공유 범위가 있습니다. 침수 도형은 로컬 지도에 표시되며 ZIP의 지도에는 해당 재배포 근거가 확인되면 포함됩니다.

지도는 구호차량 후보 도로망을 표시합니다. 시설 상태 필터는 도로의 개방 상태를 보여 줍니다. 직선 접속선은 연구용이며 실제 차량 진입 여부는 모두 미확인입니다.

코드와 재현 절차는 [Kor_map]({GITHUB}) 및 [실행 안내]({GITHUB}/blob/team-share-visualization/docs/TEAM_SHARE_PACKAGE.md)에서 확인합니다.
"""
    (light / 'README_KO.md').write_text(readme, encoding='utf-8')
    sharing = f"""# 데이터 출처와 공유 범위

도로 데이터 © OpenStreetMap contributors. Geofabrik 스냅샷 시각은 `{source['data_timestamp']}`입니다. 원본 도로 프로파일과 도로 형상은 ODbL 1.0의 출처 표시 및 해당 재배포 조건을 따릅니다. [OpenStreetMap 라이선스]({OSM_LICENSE})

내장 배경의 군 경계는 기존 OSM 행정경계 검증본을 사용합니다. 주요 도로는 검증된 S0 아티팩트에서 표시용으로 가져오고, 읍·면 등 지명은 원본 노드에 지명 태그와 대응 좌표가 있을 때 표시합니다. 재배포 근거를 확인할 수 있는 독립 수계 레이어는 확보된 입력에 없습니다. 온라인 일반 지도는 OpenFreeMap의 Liberty 스타일이며 OpenMapTiles와 OpenStreetMap 출처를 화면에 표시합니다. [OpenFreeMap 이용 조건](https://openfreemap.org/tos/)

침수 입력 출처는 기후에너지환경부 홍수위험지도 정보시스템의 국가·지방하천 자료입니다. 포털은 출처표시·상업적 이용금지·변경금지 조건을 표시합니다. 가공 자료의 팀 공유에 적용되는 허락 범위를 확인합니다. [홍수위험지도]({FLOOD_SOURCE}) · [공공누리 제4유형](https://www.kogl.or.kr/info/licenseType4.do)

시설 자료는 충청남도 이재민 임시주거시설 자료의 검증본에서 파생됐습니다. 팀 연구용 결과에는 시설의 공개 명칭·주소와 분석 좌표가 포함됩니다. 원본 시설 CSV와 연구 자료의 팀 외 배포에는 해당 이용 조건을 확인합니다.

## 확인된 포함 범위

{credits or '- 별도 권한 기록 없음'}

## 공유 ZIP 제외 자료

{excluded or '- 제외 대상 없음'}

로컬 검토용 `START_HERE.html`의 침수 도형과 공유 ZIP의 포함 범위는 구분됩니다.
"""
    (light / 'SHARING.md').write_text(sharing, encoding='utf-8')
    selections = data['final_qa']['selection_methods']
    projection = data['projection']
    method = (ROOT / 'docs' / 'TEAM_SHARE_METHODS.md').read_text(encoding='utf-8')
    for key,value in {
        'RESULTS': result_table(data['stats']),
        'SELECTION': (f"자동 투영 {selections.get('automatic_projection', 0)}곳, 수동 검토 유지 "
                      f"{selections.get('manual_keep', 0)}곳, 입구 근거 보정 "
                      f"{selections.get('manual_entrance_refinement', 0)}곳으로 확정했다."),
        'DISTANCE': (f"기존 근접도 비교에서 적격 노드까지의 중앙값은 "
                     f"{projection['nearest_eligible_node_m']['median']:.2f}m이고, 도로 형상 투영까지는 "
                     f"{projection['projection_distance_m']['median']:.2f}m였다."),
    }.items():
        method = method.replace('@@' + key + '@@', value)
    (light / 'METHODS_KO.md').write_text(method, encoding='utf-8')
    shutil.copyfile(VIEWER / 'LEAFLET_LICENSE.txt', light / 'LEAFLET_LICENSE.txt')
    write_json(light / 'SHARE_METADATA.json', dict(
        profile=PROFILE, final_run=data['final'].name, statistics=data['stats'],
        rejected_final_runs=data['rejected'], source_data_sha256={k:dict(name=data['paths'][k].name, sha256=v)
                                                                for k,v in sha.items()},
        display=display, display_simplification_m=dict(roads=ROAD_SIMPLIFY_M, flood=MASK_SIMPLIFY_M),
        online_basemap=ONLINE_BASEMAP,
        display_flood_included=rights['flood_display']['included'], excluded_from_share=omitted,
        distribution_rights=rights,
    ))


def write_manifest(path, entries):
    records = {name: (p.stat().st_size, file_sha256(p)) for name, p in entries.items()}
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        csv.writer(stream).writerow(('path', 'bytes', 'sha256'))
        csv.writer(stream).writerows((n,*records[n]) for n in sorted(records))
    return records


def zip_package(path, entries, manifest=None):
    with ZipFile(path, 'x', compression=ZIP_DEFLATED, compresslevel=6) as z:
        for name, source in sorted(entries.items()):
            z.write(source, name)
    with ZipFile(path) as z:
        require(z.namelist() == sorted(entries) and z.testzip() is None, f'ZIP integrity failure: {path.name}')
        if manifest is not None:
            for name, (size, digest) in manifest.items():
                with z.open(name) as payload:
                    require(z.getinfo(name).file_size == size
                            and hashlib.file_digest(payload, 'sha256').hexdigest() == digest,
                            f'ZIP manifest differs: {name}')


def build(args):
    output = args.output.resolve()
    require(not output.exists() and all(not output.is_relative_to(p.resolve())
                                        for p in (args.run, args.road_artifact, args.projection_dir)),
            'Use a new output folder outside the input directories')
    data = load_verified_inputs(args)
    require(not output.is_relative_to(data['final'].resolve()), 'Output points into the final run')
    rights = distribution_rights(data, args.rights)
    print(f'Validated final run: {data["final"].name}', flush=True)
    geometry = load_geometry(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.team-share-', dir=output.parent) as temp:
        scratch = Path(temp)
        publish = scratch / 'publish'
        publish.mkdir()
        light = scratch / 'light'
        light.mkdir()
        core = light / 'data'
        core.mkdir()
        local_html = publish / 'START_HERE.html'
        render_html(local_html, data, geometry, show_masks=True)
        shared_html = light / 'START_HERE.html'
        if rights['flood_display']['included']:
            shutil.copyfile(local_html, shared_html)
        else:
            render_html(shared_html, data, geometry, show_masks=False)
        shutil.copyfile(data['paths']['comparison'], core / 'scenario_comparison.csv')
        for name in FINAL_FILES:
            shutil.copyfile(data['final'] / name, core / name)
        plot_results(light / 'charts', data)
        write_documents(light, data, geometry[-1], rights)
        light_entries = {str(p.relative_to(light).as_posix()):p for p in light.rglob('*') if p.is_file()}
        full_entries = dict(light_entries, **{
            'qa/independent_road_qa.json':data['paths']['road_qa'],
            'qa/facility_access_qa.json':data['final'] / 'facility_access_qa.json',
            'qa/facility_projection_summary.json':data['paths']['projection_summary'],
            'REPRODUCE_KO.md':ROOT / 'docs' / 'TEAM_SHARE_PACKAGE.md',
            'input/road_profile_artifacts/metadata.json':data['paths']['road_meta'],
        })
        for label, input_key, dst in (
            ('base_road', 'road', 'input/road_profile_artifacts/road_network_profiles.gpkg'),
            ('base_road', 'node_tags', 'input/road_profile_artifacts/source_node_tags.json'),
            ('scenario_gpkg', 's0', 'input/scenario/road_scenarios.gpkg'),
            ('flood_gpkg', 'flood', 'input/flood/flood_100y_depth_scenarios.gpkg'),
            ('trace_s1', 'S1', f'input/scenario/{PROFILE}_s1_edge_trace.csv'),
            ('trace_s2', 'S2', f'input/scenario/{PROFILE}_s2_edge_trace.csv'),
            ('source_facilities', 'facilities', 'input/shelters_final_147_verified.csv'),
        ):
            if rights[label]['included']:
                full_entries[dst] = data['paths'][input_key]
        manifest_path = scratch / 'manifest_sha256.csv'
        manifest = write_manifest(manifest_path, full_entries)
        full_entries[manifest_path.name] = manifest_path
        zip_package(publish / 'Kor_map_team_share_light.zip', light_entries)
        zip_package(publish / 'Kor_map_team_share_full.zip', full_entries, manifest)
        shutil.copyfile(light / 'SHARING.md', publish / 'SHARING.md')
        shutil.copyfile(light / 'SHARE_METADATA.json', publish / 'SHARE_METADATA.json')
        data['hashes'].unchanged()
        require(not output.exists(), f'Output already exists: {output}')
        publish.rename(output)
    print(f'Wrote validated HTML and two ZIPs: {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'road-artifact', 'facilities', 'flood-gpkg', 'independent-qa', 'projection-dir', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--final-run', type=Path, help='Select a specific passed, matching facility folder')
    parser.add_argument('--manual-review', type=Path, help='Verify the manual-review file if available')
    parser.add_argument('--rights', type=Path, help='Hash-bound archive redistribution authorizations')
    build(parser.parse_args())


if __name__ == '__main__':
    main()
