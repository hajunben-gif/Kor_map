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
- 시설별로 가장 가까운 열린 도로와 기존 QA의 기준 도로망 앵커에 양방향으로 도달할 수 있는 도로까지의 직선거리를 계산한다. 50·100·300m 기준 집계를 함께 제공한다. 시설 좌표는 주소 대표점이고 거리에는 실제 진입로, 회전 제한 및 주행 시간이 반영되지 않는다.

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
