# 팀 공유 패키지 생성

이 기능은 PR #20으로 검증된 Windows 산출물을 읽어 오프라인 HTML 지도와 두 ZIP을 생성한다. 지도의 도로 프로파일은 `relief_vehicle_candidate`이며 비교 CSV에는 전체 프로파일이 들어 있다.

## Windows PowerShell

저장소 루트에서 실행한다. 기존 분석의 Python 가상환경을 사용한다. 그림을 생성하려면 해당 가상환경에 Matplotlib가 필요하다.

```powershell
git fetch origin
git switch team-share-visualization
git pull --ff-only origin team-share-visualization
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install matplotlib
if ($LASTEXITCODE -ne 0) { throw 'Matplotlib 설치 실패' }

$run = 'work/flood-road-scenarios-20261124-120849'
$roadArtifact = 'local_inputs/road_profile_artifacts'
$facilities = 'local_inputs/shelters_final_147_verified.csv'
$projection = 'work/facility-projection-review/results'
$flood = Join-Path ([Environment]::GetFolderPath('Desktop')) 'flood_depth_100y_result/flood_100y_depth_scenarios.gpkg'
$output = "work/team-share-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

$runHash = (Get-FileHash -LiteralPath (Join-Path $run 'road_scenarios.gpkg') -Algorithm SHA256).Hash
$roadQA = Get-ChildItem 'work' -Directory -Filter 'flood-road-qa-*' |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName 'qa.json' } |
    Where-Object {
        if (Test-Path -LiteralPath $_ -PathType Leaf) {
            $qa = Get-Content -LiteralPath $_ -Raw -Encoding UTF8 | ConvertFrom-Json
            $qa.status -eq 'passed' -and $qa.run_gpkg_sha256 -eq $runHash
        }
    } | Select-Object -First 1
if (-not $roadQA) { throw '도로망 독립 QA의 qa.json 경로를 $roadQA에 지정하세요.' }

& $python -m scripts.build_team_share_package `
    --run $run --road-artifact $roadArtifact --facilities $facilities `
    --flood-gpkg $flood --independent-qa $roadQA --projection-dir $projection `
    --output $output
if ($LASTEXITCODE -ne 0) { throw '팀 공유 패키지 생성 실패' }
Start-Process (Resolve-Path (Join-Path $output 'START_HERE.html')).Path
```

`$projection`은 기존 `facility_projection_results.zip`을 푼 폴더에 맞춘다. 다른 곳에 독립 QA를 저장했다면 `$roadQA`에 해당 `qa.json`을 지정한다. 직접 지정한 QA도 SHA-256을 검사한다. `--final-run`을 지정하지 않으면 `$run`과 같은 상위 폴더에서 `final-facility-access-*`를 최신 이름부터 검사한다. `passed` 상태, 4개 CSV 존재 및 해시, 입력 해시, 고정 좌표와 실제 CSV 집계를 모두 통과한 폴더를 선택한다. 특정 폴더를 쓰려면 `--final-run '경로'`를 추가한다. 기준망 출처가 S0 내보내기 사본인 최종 QA는 기존 투영 요약의 S0 사본 해시로 확인한다. 전체 GeoPackage 출처는 현재 시나리오 GeoPackage의 해시로 대조한다.

수동 검토 CSV가 준비돼 있으면 `--manual-review '경로/facility_projection_manual_review.csv'`를 추가해 해당 입력 해시도 확인할 수 있다. 패키지에는 검증된 시설 결과 CSV 4개를 복사한다.

## 지도와 ZIP

- `START_HERE.html`은 전체 로컬 지도다. 최신 Windows Edge 또는 Chrome에서 더블클릭해 연다. Leaflet 1.9.4와 지도 데이터가 파일 안에 들어 있으며, 브라우저의 gzip `DecompressionStream`을 사용한다.
- `Kor_map_team_share_light.zip`에는 팀 공유용 HTML, README, 시나리오 비교, 시설 결과 CSV 4개, 그래프, 한국어 연구 방법, 출처와 공유 범위가 있다.
- `Kor_map_team_share_full.zip`에는 경량판의 모든 항목과 원본 도로 프로파일 아티팩트, QA, 재현 안내가 들어 있다. 다른 원본은 아래 재배포 조건에 따라 추가된다. `manifest_sha256.csv`는 매니페스트 자체를 제외한 ZIP 내부 파일별 크기와 SHA-256이다.
- 생성 폴더의 `SHARING.md`와 ZIP 내부의 동명 문서에서 포함 및 제외 사유를 확인한다. 기존 결과 폴더와 데이터는 입력 SHA-256으로 검증한다.

배경지도는 끈 상태로 시작한다. 켜면 [EOX Terrain Light](https://maps.eox.at/)의 현재 화면 타일을 요청한다. 화면에 OSM·EOX 출처를 표시하고 타일을 저장하거나 ZIP에 묶지 않는다. 서비스 연결에 실패해도 저장된 도로와 시설을 표시한다. 도로 도형을 시나리오별로 압축하고 처음 고른 시나리오부터 해제한다. 표시용 단순화는 도로 1.5m, 침수 마스크 10m의 EPSG:5186 기준을 적용하고 Canvas로 현재 화면과 겹치는 형상을 그린다. 분석 집계는 기존 QA와 CSV에서 읽는다.

## 재배포 범위

[홍수위험지도 포털](https://www.floodmap.go.kr/natreg/)은 [공공누리 제4유형](https://www.kogl.or.kr/info/licenseType4.do)의 이용 조건을 표시한다. 공유 ZIP의 침수 도형은 변경 및 팀 배포 근거가 확보되면 포함한다. 근거가 없으면 공유판에서도 도로 상태, 침수 관련 수치, 시설 위치와 시설 판정을 표시하며 침수 도형 레이어는 비활성이다. 로컬 확인과 팀 공유는 파일명 `START_HERE.html`이 같으므로, 팀원에게는 ZIP 안의 버전을 전달한다.

원본 침수 GeoPackage, 침수 시나리오 GeoPackage, S1·S2 trace 및 원본 시설 CSV는 각각의 재배포 근거가 있을 때 전체판에 넣는다. 재배포 근거가 확인된 파일만 UTF-8 JSON에 기록하고 `--rights '경로/rights.json'`을 실행 명령에 추가한다. 아래 값은 자리 표시자다. 실제 SHA-256과 공개 가능한 허락 근거 및 출처 표기로 바꾼다.

```json
{
  "flood_display": {
    "sha256": "flood_100y_depth_scenarios.gpkg의 SHA-256",
    "basis": "침수 도형 단순화 및 팀 배포 허락 문서",
    "credit": "출처 표기"
  }
}
```

추가 키는 `flood_gpkg`, `scenario_gpkg`, `trace_s1`, `trace_s2`, `source_facilities`다. 전체판의 기본 도로 프로파일과 노드 태그는 기존 출처 메타데이터가 ODbL 1.0을 명시할 때 포함한다. 공유판에서 제외된 원본은 재현 PC에 권한을 확보해 별도로 준비한다.

## 확인

해시와 분석 집계는 `SHARE_METADATA.json`에 기록한다. 실데이터 기준은 동일한 접속 위치 146개, S0 개방 146, S1 개방 130·차단 16, S2 개방 128·차단 18이다. 시나리오 버튼, 차단 필터, 도로 유형과 레이어 전환을 Windows Edge 또는 Chrome에서 확인한다. 주요 화면의 수치는 기존 검증값을 사용한다.

합성 데이터 테스트는 `python -m pytest -q tests/test_build_team_share_package.py`로 실행한다. Playwright와 Chromium이 설치된 환경에서 `TEAM_SHARE_CHROMIUM`에 Chromium 실행 파일 경로를 지정하면 브라우저 테스트도 실행된다. 실제 Windows 입력과 브라우저 검증은 위 명령으로 별도로 수행한다.
