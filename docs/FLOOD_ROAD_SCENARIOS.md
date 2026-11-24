# 부여·청양 침수 도로망 분석

검증된 침수 마스크와 2026년 11월 20일 방향성 도로망 3종으로 S0·S1·S2를 계산한다. 분석 입력과 결과는 로컬에 보관한다.

## 입력

| 파일 | 위치 및 출처 |
|---|---|
| `flood_100y_depth_scenarios.gpkg` | `local_inputs/flood_depth_100y_result/` 또는 Windows 바탕화면의 `flood_depth_100y_result` |
| `flood_depth_qa.json` | 저장소의 `results/flood_100y/` |
| `road_network_profiles.gpkg`, `metadata.json` | GitHub Actions `road-profile-comparison-44992793405-1`에서 꺼내 `local_inputs/road_profile_artifacts/`에 저장 |
| 시설 147개 CSV | 저장소의 `data/processed/shelters_final_147.csv` |

도로망 ZIP의 게시 SHA-256은 `59e54be3a566c7ef44d5ad6192c2440c5b356518e75882ca7d75a4b45db9158c`다. 실행 시 GeoPackage와 시설 CSV를 Artifact 메타데이터의 파일 해시와 비교한다. [GitHub Actions 실행 기록](https://github.com/hajunben-gif/Kor_map/actions/runs/44992793405)

## Windows PowerShell 실행

GitHub Actions 실행 기록에서 `road-profile-comparison-44992793405-1` ZIP을 다운로드한다. 아래 예시는 ZIP을 다운로드 폴더에 저장하고, 저장소를 `Documents\Kor_map`에 체크아웃한 경우다. 다른 위치라면 해당 변수만 수정한다. 현재 작업 브랜치를 체크아웃한 상태에서 실행한다.

```powershell
Set-Location (Join-Path $env:USERPROFILE 'Documents\Kor_map')
$ErrorActionPreference = 'Stop'

$zip = Join-Path $env:USERPROFILE 'Downloads\road-profile-comparison-44992793405-1.zip'
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne '59e54be3a566c7ef44d5ad6192c2440c5b356518e75882ca7d75a4b45db9158c') {
    throw '도로망 Artifact ZIP의 SHA-256이 게시값과 다릅니다.'
}
$artifactDir = Join-Path $PWD.Path 'local_inputs\road_profile_artifacts'
Expand-Archive -LiteralPath $zip -DestinationPath $artifactDir

$flood = Join-Path ([Environment]::GetFolderPath('Desktop')) 'flood_depth_100y_result\flood_100y_depth_scenarios.gpkg'
$floodQA = Join-Path $PWD.Path 'results\flood_100y\flood_depth_qa.json'
$road = Join-Path $artifactDir 'road_network_profiles.gpkg'
$metadata = Join-Path $artifactDir 'metadata.json'
foreach ($file in @($flood, $floodQA, $road, $metadata)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "입력 파일이 없습니다. $file" }
}

py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 가상환경 생성 실패' }
$python = Join-Path $PWD.Path '.venv\Scripts\python.exe'
& $python -m pip install -r requirements.txt -r requirements-pbf-audit.txt
if ($LASTEXITCODE -ne 0) { throw '패키지 설치 실패' }

$run = Get-Date -Format 'yyyyMMdd-HHmmss'
$floodCheck = Join-Path $PWD.Path "work\flood-input-check-$run"
$output = Join-Path $PWD.Path "work\flood-road-scenarios-$run"
& $python -m scripts.build_flood_road_scenarios --flood "$flood" --flood-qa "$floodQA" --validate-flood-only --output "$floodCheck"
if ($LASTEXITCODE -ne 0) { throw '침수 GeoPackage 검증 실패' }
& $python -m scripts.build_flood_road_scenarios --flood "$flood" --flood-qa "$floodQA" --road-gpkg "$road" --road-metadata "$metadata" --output "$output"
if ($LASTEXITCODE -ne 0) { throw '도로망 시나리오 분석 실패' }

Get-Content -LiteralPath (Join-Path $floodCheck 'flood_input_validation.json') -Encoding UTF8
Get-Content -LiteralPath (Join-Path $output 'validation.json') -Encoding UTF8
Get-Content -LiteralPath (Join-Path $output 'report_ko.md') -Encoding UTF8
```

이미 ZIP을 풀어 둔 경우 `$artifactDir`을 해당 디렉터리로 지정하고 다운로드 검증·압축 해제 단계를 생략한다. 두 도로 파일은 서로 다른 디렉터리에도 둘 수 있다. `--road-artifact`로 기존처럼 두 파일이 든 디렉터리를 지정할 수 있으며, `--road-gpkg`와 `--road-metadata`는 각각 해당 경로를 우선한다. 다른 시설 CSV 경로가 필요하면 `--facilities`로 지정한다. 시설 CSV의 SHA-256은 검증된 도로망 Artifact의 기록과 일치해야 한다.

GeoPackage와 실행 결과는 Git에서 제외되는 `local_inputs/` 또는 `work/`에 보관한다. 실행 로그에는 레이어 검증, 프로필 처리량, 시나리오 내보내기 진행 상황이 표시된다.

## 테스트 및 기존 SHP 감사 오류

해당 브랜치에서 추가한 합성 테스트 4건이 통과했다. 클라우드 전체 테스트 47건에서는 기존 `test_reject_incorrect_crs` 1건이 오류를 냈다. 이 환경은 Python 3.12, pyproj 3.8.0, PROJ 9.8.1을 사용했다. 브랜치 변경 전 기준 커밋 `2c4e9d8`의 테스트와 감사 스크립트에서도 동일한 오류를 재현했다.

테스트의 `GEOGCS["WGS 84"]`는 CRS가 다른 정상 WKT를 표현하는 데 필요한 DATUM 또는 ENSEMBLE을 포함하지 않는다. PROJ가 입력을 거부해 EPSG 비교 전에 `pyproj.exceptions.CRSError`가 발생하고, 테스트는 `ValueError`를 기대한다. 현재 파서가 잘못된 WKT를 거부하는 방식과 기존 테스트의 예외 기대값이 맞지 않는 사례다. 이 브랜치의 분석 코드 변경으로 인한 회귀는 기준 커밋 재현 결과에서 확인되지 않았다. 기존 테스트와 감사 코드는 유지했다.

```powershell
& $python -c "import pyproj; print(pyproj.__version__, pyproj.proj_version_str)"
& $python -m unittest discover -s tests -p test_build_flood_road_scenarios.py
& $python -m unittest discover -s tests -p test_flood_shp_100y.py
& $python -m unittest discover -s tests
```

변경 전 결과는 별도 체크아웃 `git worktree add --detach ..\Kor_map-shp-baseline 2c4e9d8`에서 같은 Python 실행 파일 및 SHP 감사 테스트 명령으로 재현할 수 있다. Windows 실데이터 분석과 실행 결과 검증은 이후 로컬에서 수행한다.

## 판정 및 집계

- EPSG:5186에서 침수 마스크를 공간 인덱싱해 도로의 각 원본 OSM 선분과 교차한다. 내부 중첩에 양의 길이가 있을 때만 침수 구간으로 분류한다. 경계 접촉만 있는 간선은 CSV에 남긴다.
- S0는 기준망, S1은 N331~N334, S2는 N330~N334를 사용한다. 교량이나 고가 구조물의 중첩은 `review`로 표시하고 통과 가능한 것으로 가정한다. 실제 차량 통행은 현장 조건과 통제 여부를 별도로 확인해야 한다. 국가·지방하천 위험지도의 결합은 합성 시나리오다.
- 열린 부분은 원래 이동 방향을 보존한다. 침수 경계의 새 노드는 해당 방향 간선에만 연결해 가상의 회전이나 도로 간 접속을 막는다. WCC와 SCC는 열린 방향 간선의 끝점 기준이며 원본 노드의 소속 변화는 대응표에서 확인한다.
- 물리 도로 연장은 프로필별 OSM way ID와 원본 선분 순번으로 양방향 중복을 제거한다. 별개 way로 매핑된 차도는 각각 센다. 방향별 집계는 방향 간선의 중첩 길이를 합한다.
- 시설 대표점에서 열린 도로 및 기준 SCC의 시설 접속 후보 도로까지의 직선거리를 계산한다. 50·100·300m 기준 집계를 함께 제공한다. 세부 정의와 주행용 OD 준비 절차는 아래에 있다.

## 출력

| 파일 | 내용 |
|---|---|
| `flood_input_validation.json` | SQLite, 레이어·CRS, 면적 및 시나리오 포함 관계 |
| `road_scenarios.gpkg` | 9개 프로필·시나리오 조합의 열린 노드와 방향 간선 |
| `*_road_impact.csv`, `*_edge_trace.csv` | 원본 간선의 침수 영향, 분할 조각의 상태 및 원본 ID 대응 |
| `*_node_components.csv` | 기존 노드의 S0·시나리오 WCC/SCC 대응과 단절 노드 |
| `scenario_comparison.csv`, `road_type_comparison.csv` | 차단 및 연결성 전후 비교와 도로 유형별 집계 |
| `facility_access_comparison.csv` | 좌표가 확보된 146개 시설의 거리·접속 후보·기준 대비 차이 |
| `validation.json`, `report_ko.md` | 해시와 검증 결과, 한국어 분석 요약 |
| `flood_road_map.html` | 시각화용 침수 마스크와 구호차량 후보망 영향 구간 |

## 시설 거리와 주행 도달성

| 항목 | 정의 |
|---|---|
| `nearest_open_road_distance_m` | 시설 대표점에서 열린 양의 길이 방향 간선 형상까지의 최소 평면 직선거리. 기존 열명은 `nearest_distance_m`. |
| `reference_core_road_distance_m` | 같은 직선거리 계산에서 선택된 기준 SCC에 속한 간선만 사용한다. 시설 접속 후보 집합에서는 motorway, trunk 및 그 연결로를 제외한다. 기존 열명은 `connected_distance_m`. |
| `network_distance_m` | 고정 창고 노드에서 확인된 시설 진입점 노드까지 방향 그래프의 EPSG:5186 간선 거리 합을 최소화한 값. 시설 대표점까지의 직선거리는 더하지 않는다. |
| `unreachable_count` | 창고 노드와 시나리오별로, 진입점이 확인된 수요시설 중 해당 방향 경로가 없는 시설 수. 진입점 미확인 건수는 `entrance_unverified_count`에 기록한다. |

`reference_core`는 S0 일반 차량망의 최대 WCC에서 일반 도로 앵커가 가장 많이 들어 있는 SCC를 골라 기준 앵커 후보를 고정한다. `active_reference_anchors`는 각 프로필과 시나리오에서 일반 도로를 포함하는 현재 SCC마다 남은 기준 앵커 수를 세고, 앵커 수·SCC 크기·최소 노드 ID 순으로 SCC 하나를 고른다. `edge_eligibility`는 선택된 SCC에서 간선 시작점에 도달하고 간선 끝점에서 같은 SCC로 복귀할 수 있는지를 검사한다. 이때 두 끝점은 같은 SCC에 속한다. `evaluate_facilities`는 각 거리 대상의 간선 형상을 공간 인덱싱한다.

침수 범위가 확대되어 S1의 SCC가 더 많이 쪼개지면, S2에서 다른 기준 SCC가 선택되어 일부 시설의 직선거리가 줄 수 있다. 동일한 앵커가 두 선택 SCC에 남아 있으면 S2의 선택 도로는 S1의 선택 도로 형상 안에 있으므로 이 감소를 기대하지 않는다. 기존 `facilities_connected=146`은 146개 좌표 각각에 유한한 기준망 도로 직선거리가 산출되었다는 뜻이다. 새 집계 이름은 `facilities_with_reference_core_road`이며, 시설까지의 차량 경로 존재 여부는 OD에서 계산한다.

## 기존 Windows 결과의 독립 QA

입력은 기존 실행 폴더의 `validation.json`, `road_scenarios.gpkg`, 9개 조합의 `*_edge_trace.csv`와 `*_node_components.csv`, `facility_access_comparison.csv`, 그리고 당시 원본 도로 프로필 GeoPackage·메타데이터·시설 CSV다. 최초 침수 GeoPackage나 도형 교차 연산은 필요하지 않다. 기존 열명과 새 열명을 모두 읽는다.

`qa.json`은 원본 방향·길이·OSM 식별자와 저장 간선의 대응, 방향별 분할점의 독점 사용, 양방향을 정규화한 OSM way·선분 순번별 침수 구간의 포함 관계를 검사한 결과를 기록한다. 교량·고가도로 등 태그에 따른 review 상태와 양방향 일관성도 검사한다. 원본 도로에 이미 있던 접점과 일방통행을 검증하며, 신호나 OSM 회전 제한 relation의 합법성 감사 범위는 분할로 생긴 접점 검사와 별개다. 두 시나리오에서 모두 살아남은 원본 노드들의 WCC/SCC가 병합되지 않았는지도 검사한다.

WCC는 방향을 무시한 연결요소이고 SCC는 서로 왕복 가능성이 있는 방향 연결요소다. 분할 경계 노드가 추가되고 간선이 없는 원본 노드가 빠지므로 활성 그래프의 요소 수만으로 원본 연결성 변화를 해석하기 어렵다. QA에는 활성 그래프의 요소 수, 누락된 원본 노드를 단일 요소로 복원한 수, 최대 요소의 원본 노드 수를 함께 기록한다. `reference_selection`, `shared_s1_s2_anchors`, `anchor_proximity_decreases`에서 기준 SCC 전환과 영향을 받은 시설별 거리를 확인할 수 있다.

```powershell
$python = '.\.venv\Scripts\python.exe'
$existing = 'work/flood-road-scenarios-20261124-120849'
$road = 'local_inputs/road_profile_artifacts/road_network_profiles.gpkg'
$metadata = 'local_inputs/road_profile_artifacts/metadata.json'
$facilities = 'data/processed/shelters_final_147.csv'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

& $python -m scripts.qa_flood_road_scenarios --run $existing --road-gpkg $road --road-metadata $metadata --facilities $facilities --output "work/flood-road-qa-$stamp"
if ($LASTEXITCODE -ne 0) { throw '독립 도로망 QA 실패' }
Get-Content "work/flood-road-qa-$stamp/qa.json" -Encoding UTF8
```

## 입지 최적화 전 OD 준비

- 창고 후보는 `relief_vehicle_candidate` S0의 원본 그래프 노드 전체다. 좌표는 EPSG:5186이며, 분할 경계 노드를 후보로 추가하거나 S1·S2에서 후보를 이동하지 않는다. 사유지·건축 가능성·상하차 승인은 별도 입력이다.
- 수요지 키는 시설 CSV의 `시군명`과 `시설일련번호` 조합이다. 시설 좌표는 진입점 조사에 사용하고 시나리오별 최근접 노드 배정은 하지 않는다. 로컬 진입점 CSV의 열은 아래와 같다. 좌표가 있는 146개 시설 각각에 한 행을 둔다. `verified`에는 실제 차량 진입로와 직접 대응하는 S0 원본 노드 ID와 확인 근거를 기록한다. 동일 위치에 서로 다른 방향의 노드가 있다면 실제 진입 가능한 노드를 확인한다. `unverified`에는 노드 ID를 비워 둔다. 현재 형식은 시설당 고정 진입점 하나를 모델링한다.
- 각 시설 진입점의 역방향 그래프에서 Dijkstra를 실행하면 모든 창고 후보에서 해당 진입점까지의 원래 방향 최단거리를 얻는다. 진입점에 들어오는 열린 간선이 있어야 도착으로 인정한다. 분할 경계에서는 다른 간선으로 옮길 수 없으며 일방통행은 보존된다. 원래 도로 접점의 회전 제한 relation, 조건부 통행, 차단기·통행권, 실제 구호차량 규격과 주행 시간은 현재 그래프의 모델 범위 밖이다.
- 침수와 중첩한 교량·고가 구간은 기본 OD에서 차단하는 보수적 가정을 사용한다. `--review-policy open`은 기존 분석과 동일하게 review 간선을 열어 두는 민감도 분석이다. 한 실행에서는 정책과 시설 진입점을 모든 시나리오에 고정한다.
- OD 행의 키는 `warehouse_node_id, county, facility_id, scenario`다. 도달 불가능한 쌍의 거리 필드는 비우고 `reachable=0`을 기록한다. 진입점이 미확인된 쌍은 거리와 `reachable`을 비워 둔다. 내부 검증에서 도달 불가능은 무한대로 표현한다. 같은 출발·도착점에서 S2가 S1보다 짧아지거나 S1에서 없던 경로가 S2에서 생기면 실패한다. S2에서 간선 부분을 추가로 제거해도 사라졌던 경로가 생길 수 없으므로 성립하는 조건이다.
- 결과는 `warehouse_nodes.csv`, `od.csv.gz`, `unreachable_counts.csv`, `od_manifest.json`이다. 모든 후보·시설·시나리오 조합을 스트리밍한다. 일부 진입점만 확인된 경우는 부분 OD이며, 146개 전체 수요를 대상으로 쓰려면 모든 진입점을 확인해야 한다. 시설별 수요량은 별도 실측 입력으로 남겨 둔다.

```csv
county,facility_id,entrance_node_id,entrance_status,evidence
```

Windows에서 진입점 CSV를 `local_inputs/facility_entrances.csv`에 준비한 뒤 실행한다. 이 명령은 저장 파일 독립 QA를 다시 실행하고 OD를 생성한다.

```powershell
& $python -m scripts.prepare_flood_road_od --run $existing --road-gpkg $road --road-metadata $metadata --facilities $facilities --entrances 'local_inputs/facility_entrances.csv' --output "work/flood-road-od-$stamp"
if ($LASTEXITCODE -ne 0) { throw '고정 진입점 OD 생성 또는 검증 실패' }
```

현재 Windows 실행의 보고된 `passed`는 최초 분석의 검증 결과다. 입지 최적화용 입력으로 승인하려면 해당 Windows 폴더에서 독립 QA와 고정 진입점 OD 검증을 통과하고, 사용할 구호차량과 통행·차량 진입 가정을 확인해야 한다. 이 단계에서는 입지 최적화를 실행하지 않는다.

## 시설 차량 진입점 후보와 검토 자료

이 단계는 기존 Windows 실행 폴더의 `road_scenarios.gpkg`에서 `relief_vehicle_candidate_s0_nodes`와 `relief_vehicle_candidate_s0_edges`만 읽는다. 기존 `validation.json`, 독립 QA의 `qa.json`, Windows 분석에 사용한 `local_inputs/shelters_final_147_verified.csv`의 해시를 대조한다. 침수 도형 처리나 OD 계산은 수행하지 않는다. 선택 사항인 `source_node_tags.json`은 원본 도로 프로필 아티팩트의 `metadata.json`에 기록된 해시로 확인한다. 두 파일을 생략하면 S0에 내장된 도로 태그와 원본 노드 ID는 조사할 수 있으며, 노드 단위 진입·차단기 태그는 확인할 수 없다.

### 후보 선정

1. EPSG:5186에서 시설 대표점과 가장 가까운 S0 도로 형상, 그 도로 위 투영점, 150m 안의 S0 원본 노드를 공간 인덱스로 조회한다. 양방향 형상은 OSM way와 방향을 제거한 형상으로 중복을 줄인다. 최단거리가 같은 서로 다른 way는 동률로 표시한다. 150m는 검토자가 인접 필지와 주변 도로의 대안을 볼 수 있게 둔 검색 범위다. 시설 대표점과 실제 진입로가 멀면 범위 밖에 있을 수 있다.
2. 후보별로 연결된 방향 간선과 도로 분류·OSM 태그, 원래 S0 유입·유출 차수 및 WCC·SCC를 기록한다. 검토용 연결성 계산에서는 motorway·trunk 및 연결로, 교량·고가, 명시적 차량 제한 또는 차단기 근거가 있는 방향 간선을 제외한다. 모호한 목적·조건부 허용, 표면이나 차량 제원 제한, 일방통행 등의 태그는 주의사항으로 남긴다. 간선 위 원본 노드에 표시된 제한도 반영한다. 원본 S0의 가장 큰 SCC 안에서 이 기준을 적용한 가장 큰 SCC를 공통 왕복 기준으로 사용한다.
3. 접근성 기준을 통과한 연결 도로가 있는 후보와 이 공통 기준으로 왕복하는 후보를 우선한다. 다음 순서는 차량 허용 태그가 있는 OSM 진입 노드와 연결 driveway가 모두 있는 경우, 차량 허용 태그가 있는 진입 노드, driveway, 기타 entrance 또는 service, 일반 S0 노드다. 같은 순위에서는 직선거리와 OSM 노드 ID로 결정하며 시설당 최대 5개를 출력한다. 연결 도로가 모두 제외된 노드는 검토 후보 행에 남을 수 있으며 추천 대상에서는 제외한다.
4. 최상위 태그 근거를 갖추고 25m 이내이며 접근·방향·연결성 주의사항이나 경쟁 근거가 없으면 `high_confidence_candidate`를 부여한다. 25m는 주소 대표점 주변에 강한 지도 근거가 함께 있는 경우를 우선 검토하기 위한 범위다. 추천 후보 거리보다 15m 더 먼 범위까지 비슷한 근거가 있으면 경쟁 근거로 취급한다. 15m는 대표점 위치의 작은 차이로 후보가 바뀔 수 있는 경우를 검토 대상으로 남기기 위한 여유다. 검색 범위 안에 추천 가능한 노드는 있으나 이 기준이 부족하면 `needs_review`, 추천 가능한 노드가 없으면 `unresolved`다. 거리 기준은 현장 데이터로 보정한 정확도나 확률이 아니다.
5. 최근접 도로의 투영점이 간선 중간이면 `projection_without_s0_node`로 기록한다. 투영점 5m 안에서 동일 간선의 생략된 원본 정점을 표시하고, 시설 150m 안에 생략된 `entrance` 태그 정점이 있으면 별도 검토 사유를 추가한다. 5m는 투영점 근처의 원본 정점을 함께 보여 주는 검토 범위다. 투영점은 기하학적 추정이며, 건물 진입 태그는 보행용일 수 있다. 누락 정점·중간 투영점이 실제 차량 진입점으로 확인되면 해당 원본 way와 방향의 대응을 기록하고 별도의 검증된 라우팅 입력에서 연결점을 마련한다. 하나의 동일 평면 도로에 연결해야 하며, 방향별 분할과 시나리오 차단 구간 상속 및 가상 회전 방지를 다시 검증한다. 원본 S0와 기존 분석 결과는 그대로 보관한다.

모든 자동 결과는 검토 우선순위다. 입구가 다른 필지나 건물을 가리키는지, 차로 방향과 회전이 맞는지, 사유지 허가·문·차단기·차폭·차고·하중이 해당 차량에 맞는지는 지도, 기관 정보 또는 현장으로 확인한다. OSM에서 누락된 진입로, 후보 프로필에서 제외된 도로, 원래 OSM 회전 제한이나 현장 통제를 이 도구로 확정할 수 없다. 대응하는 S0 노드와 실제 차량 진입 근거를 확인한 시설만 별도 완성본 양식에 노드 ID와 근거를 적고 `verified`로 바꾼다.

| 파일 | 내용 |
|---|---|
| `facility_entrance_candidates.csv` | 후보별 순위·좌표·거리·연결성·태그·선정 근거·주의사항 |
| `facility_entrance_review.csv` | 시설별 추천과 대체 노드, 검토 상태·사유, 최근접 도로 및 누락 정점 단서 |
| `facility_entrance_review.html` | 외부 지도 서비스가 필요 없는 검토 지도. 상태를 필터링하고 시설을 선택하면 후보·누락 단서와 주변 S0 도로가 표시된다. |
| `facility_entrances_template.csv` | 기존 OD 입력 형식의 146개 행. 모든 행은 `unverified`로 시작하며 진입점 ID를 비워 둔다. |
| `entrance_candidate_qa.json` | 입력·출력 해시, 누락·중복·S0 노드 검증, 상태별 건수와 거리 분포, 추가 검토 목록 및 기준 |

### Windows PowerShell

저장소 루트의 PR 브랜치에서 실행한다. 아래 명령은 독립 QA 출력 폴더 중 같은 GeoPackage 해시를 가진 통과 결과를 찾는다. 다른 곳에 저장한 경우 `$qaPath`를 직접 지정한다.

```powershell
git switch flood-road-scenarios
git pull --ff-only origin flood-road-scenarios
$python = '.\.venv\Scripts\python.exe'
$existing = 'work/flood-road-scenarios-20261124-120849'
$facility = 'local_inputs/shelters_final_147_verified.csv'
$artifact = 'local_inputs/road_profile_artifacts'
$gpkgHash = (Get-FileHash "$existing/road_scenarios.gpkg" -Algorithm SHA256).Hash
$qaPath = Get-ChildItem 'work' -Directory -Filter 'flood-road-qa-*' |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName 'qa.json' } |
    Where-Object {
        if (Test-Path -LiteralPath $_ -PathType Leaf) {
            $qa = Get-Content -LiteralPath $_ -Raw -Encoding UTF8 | ConvertFrom-Json
            $qa.status -eq 'passed' -and $qa.run_gpkg_sha256 -eq $gpkgHash
        }
    } | Select-Object -First 1
if (-not $qaPath) { throw '이 GeoPackage에 대응하는 독립 QA의 qa.json 경로를 지정하세요.' }

$output = "work/facility-entrance-candidates-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
& $python -m scripts.build_facility_entrance_candidates --run $existing --scenario-qa $qaPath --facilities $facility --node-tags "$artifact/source_node_tags.json" --road-metadata "$artifact/metadata.json" --output $output
if ($LASTEXITCODE -ne 0) { throw '진입점 후보 생성 또는 QA 실패' }
Get-Content -LiteralPath "$output/entrance_candidate_qa.json" -Encoding UTF8
Start-Process (Resolve-Path "$output/facility_entrance_review.html")
```

원본 프로필의 태그 파일을 확보하지 못한 경우 마지막 실행 명령에서 `--node-tags`와 `--road-metadata`를 함께 생략한다. 생성 폴더와 수동 확인 입력은 로컬에 보관한다.
