# 팀 공유 패키지 생성

이 기능은 PR #20으로 검증된 Windows 산출물을 읽어 통합 HTML 지도와 두 ZIP을 생성한다. 일반 배경지도가 기본으로 켜지며, 연결이 끊기면 내장 배경을 표시한다. 지도는 `relief_vehicle_candidate` 도로망을, 비교 CSV는 전체 프로파일을 제공한다.

## Windows PowerShell

기존 분석의 Python 가상환경을 사용한다. 그림 생성에는 Matplotlib가 필요하다.

```powershell
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath 'C:\Users\user\Documents\Kor_map'
git fetch origin
if ($LASTEXITCODE -ne 0) { throw 'Git fetch 실패' }
git switch team-share-visualization
if ($LASTEXITCODE -ne 0) { throw '팀 공유 브랜치 전환 실패' }
git pull --ff-only origin team-share-visualization
if ($LASTEXITCODE -ne 0) { throw '팀 공유 브랜치 업데이트 실패' }
$python = '.\.venv\Scripts\python.exe'

$run = 'work/flood-road-scenarios-20261124-120849'
$roadArtifact = 'local_inputs/road_profile_artifacts'
$facilities = 'local_inputs/shelters_final_147_verified.csv'
$projection = 'C:\Users\user\Downloads\facility_projection_results-77b1cbb5c8924927944e985ea77da1bb'
$flood = 'C:\Users\user\Desktop\flood_depth_100y_result\flood_100y_depth_scenarios.gpkg'
$manual = 'C:\Users\user\Downloads\facility_projection_manual_review.csv'
$output = "work/team-share-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

$gpkg = Join-Path $run 'road_scenarios.gpkg'
$required = @($python, $gpkg, $facilities, $flood, $manual)
$required += @('validation.json', 'scenario_comparison.csv',
    'relief_vehicle_candidate_s1_edge_trace.csv', 'relief_vehicle_candidate_s2_edge_trace.csv') |
    ForEach-Object { Join-Path $run $_ }
$required += @('road_network_profiles.gpkg', 'metadata.json', 'source_node_tags.json') |
    ForEach-Object { Join-Path $roadArtifact $_ }
$required += @('facility_projection_summary.json', 'facility_projection_distances.csv',
    'facility_projection_review_queue.csv', 'facility_projection_audit_sample.csv') |
    ForEach-Object { Join-Path $projection $_ }
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) } |
    ForEach-Object { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($_) })
if ($missing.Count) { throw ("필수 파일 누락" + [Environment]::NewLine + ($missing -join [Environment]::NewLine)) }

$runHash = (Get-FileHash -LiteralPath $gpkg -Algorithm SHA256).Hash
$roadQA = Get-ChildItem 'work' -Directory -Filter 'flood-road-qa-*' |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName 'qa.json' } |
    Where-Object {
        if (Test-Path -LiteralPath $_ -PathType Leaf) {
            $qa = Get-Content -LiteralPath $_ -Raw -Encoding UTF8 | ConvertFrom-Json
            $qa.status -eq 'passed' -and $qa.run_gpkg_sha256 -eq $runHash
        }
    } | Select-Object -First 1
if (-not $roadQA) { throw "일치하는 독립 QA 없음. 확인 경로 $PWD\work\flood-road-qa-*\qa.json" }

& $python -m pip install matplotlib
if ($LASTEXITCODE -ne 0) { throw 'Matplotlib 설치 실패' }
& $python -m scripts.build_team_share_package `
    --run $run --road-artifact $roadArtifact --facilities $facilities `
    --flood-gpkg $flood --independent-qa $roadQA --projection-dir $projection `
    --manual-review $manual --output $output
if ($LASTEXITCODE -ne 0) { throw '팀 공유 패키지 생성 실패' }
Start-Process (Resolve-Path (Join-Path $output 'START_HERE.html')).Path
```

다른 곳에 독립 QA를 저장했다면 `$roadQA`에 해당 `qa.json`을 지정한다. 빌더는 `$run`과 같은 상위 폴더에서 `final-facility-access-*`를 최신 이름부터 검사한다. `passed` 상태, CSV 4개의 존재 여부와 해시, 수동 검토 CSV를 포함한 입력 해시, 고정 좌표와 실제 CSV 집계를 통과한 폴더를 선택한다. 특정 폴더를 쓰려면 `--final-run '경로'`를 추가한다. S0 내보내기 사본을 사용한 최종 QA는 기존 투영 요약의 사본 해시로, 전체 GeoPackage를 사용한 최종 QA는 현재 시나리오 GeoPackage의 해시로 대조한다. 선택한 폴더와 제외 사유는 `SHARE_METADATA.json`에 기록한다.

### 기존 Windows 체크아웃의 행정경계 복구

`SHA-256 mismatch: boundary.geojson`이 발생하면 `core.autocrlf=true`인 체크아웃의 줄바꿈을 확인한다. 경계 두 파일의 Git 원본은 LF이며 `output/integrated_area/boundaries_20261113/validation.json`의 `sources` 해시와 일치한다. 기존 체크아웃에서는 줄바꿈이 CRLF인 사본이 남을 수 있다. 아래 명령은 스테이징된 변경이나 줄바꿈 외의 편집이 있으면 중단한다. 이후 두 작업 파일을 임시 폴더에 백업하고 인덱스에서 해당 파일만 다시 꺼낸 뒤, 실제 바이트의 SHA-256을 기존 QA와 대조한다.

```powershell
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath 'C:\Users\user\Documents\Kor_map'
git switch team-share-visualization
if ($LASTEXITCODE -ne 0) { throw '팀 공유 브랜치 전환 실패' }
git pull --ff-only origin team-share-visualization
if ($LASTEXITCODE -ne 0) { throw '팀 공유 브랜치 업데이트 실패' }

$archive = 'data/20261109T045434949410Z'
$counties = @('buyeo', 'cheongyang')
$paths = @($counties | ForEach-Object { "$archive/$_/boundary.geojson" })
$attributes = @(git check-attr text -- $paths)
if ($LASTEXITCODE -ne 0 -or $attributes.Count -ne $paths.Count -or
    ($attributes | Where-Object { $_ -notlike '*: text: unset' })) {
    throw '두 경계에 -text 속성이 적용됐는지 확인하세요.'
}
git diff --cached --quiet -- $paths
if ($LASTEXITCODE -ne 0) { throw '스테이징된 경계 변경을 먼저 확인하세요.' }
git diff --no-ext-diff --no-textconv --ignore-cr-at-eol --exit-code HEAD -- $paths
if ($LASTEXITCODE -ne 0) { throw '줄바꿈 외의 경계 변경을 먼저 확인하세요.' }

$backup = Join-Path ([IO.Path]::GetTempPath()) ('Kor_map-boundaries-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backup | Out-Null
foreach ($county in $counties) {
    Copy-Item -LiteralPath "$archive/$county/boundary.geojson" -Destination (Join-Path $backup "$county.geojson")
}
Write-Output "복구 전 사본 $backup"
git checkout-index --force -- $paths
if ($LASTEXITCODE -ne 0) { throw '행정경계 복구 실패' }

$qa = Get-Content -LiteralPath 'output/integrated_area/boundaries_20261113/validation.json' -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($county in $counties) {
    $path = "$archive/$county/boundary.geojson"
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($hash -ne $qa.sources.$county.sha256) { throw "기존 QA 해시 불일치 $path" }
    Write-Output "$county $hash"
}
```

두 해시가 일치하면 위의 패키지 생성 명령을 다시 실행한다. `git ls-files --eol -- $paths`로 확인할 때 두 파일에 `i/lf w/lf attr/-text`가 표시된다.

같은 설정에서 원본 통합망 빌드의 `output/integrated_area/boundaries_20261113/buffer_2000m_epsg5179.geojson`과 저장소의 `data/processed/shelters_final_147.csv`도 CRLF로 바뀌면 검증 해시가 달라진다. 팀 공유 패키지는 버퍼 파일을 읽지 않으며, 위 명령에서 시설 입력은 `local_inputs/shelters_final_147_verified.csv`를 사용한다. 기존 시나리오·시설 산출물과 압축을 푼 도로 아티팩트는 검증된 바이트 그대로 사용한다.

## 지도와 ZIP

- `START_HERE.html`은 전체 로컬 지도다. 최신 Windows Edge 또는 Chrome에서 더블클릭해 연다. Leaflet 1.9.4와 지도 데이터가 파일 안에 들어 있으며, 브라우저의 gzip `DecompressionStream`을 사용한다.
- `Kor_map_team_share_light.zip`에는 팀 공유용 HTML, README, 시나리오 비교, 시설 결과 CSV 4개, 그래프, 한국어 연구 방법, 출처와 공유 범위가 있다.
- `Kor_map_team_share_full.zip`에는 경량판의 모든 항목과 원본 도로 프로파일 아티팩트, QA, 재현 안내가 들어 있다. 다른 원본은 아래 재배포 조건에 따라 추가된다. `manifest_sha256.csv`는 매니페스트 자체를 제외한 ZIP 내부 파일별 크기와 SHA-256이다.
- 생성 폴더와 두 ZIP에 `SHARE_METADATA.json`과 `SHARING.md`를 제공한다. 재배포 범위, 입력 SHA-256, 시나리오 통계와 내장 배경 출처를 확인할 수 있다.

## 배경지도와 연결 실패

기본 배경은 OpenFreeMap Liberty다. 도로명·지명·하천·건물 등의 위치 정보를 분석 레이어 아래에 표시한다. 인터넷과 WebGL을 사용하며 MapLibre GL JS 5.24.1을 jsDelivr에서 불러온다. OpenFreeMap은 키 없이 공개 스타일을 앱에 삽입할 수 있도록 안내하고 서비스 가용성은 보증하지 않는다. 지도에는 OpenFreeMap, OpenMapTiles, OpenStreetMap 출처를 표시한다. 요청은 브라우저에서 서비스로 직접 전송되며 온라인 타일은 ZIP에 넣지 않는다. [OpenFreeMap 사용법](https://openfreemap.org/quick_start/) · [이용 조건](https://openfreemap.org/tos/)

EOX도 링크와 출처 표시를 조건으로 앱 사용을 안내한다. Terrain Light의 지명과 도로 표시는 별도 레이어를 사용하고, 공개 서비스에는 요청 제한이 있다. 지명과 도로가 기본 스타일에 포함되고 기존 Windows 검증 이력이 있는 OpenFreeMap을 선택했다. [EOX 서비스 안내](https://maps.eox.at/)

이전 HTML은 로컬 파일에서 유효한 Referer 없이 OSM 공개 래스터 서버를 호출해 403 차단 타일이 표시됐다. PR #6은 요청을 중단하고 검증된 군 경계를 내장했다. 후속 PR #8·#9에서 OpenFreeMap을 도입하고 라이브러리 로드 순서를 고쳤다. 이전 뷰어의 Windows Chrome·Edge 성공은 저장소에 기록돼 있다. 이번 버전도 공식 Liberty 스타일을 사용하며, Windows에서 새 HTML의 `file://` 연결은 아래 절차로 확인한다. [이전 뷰어 이력](./VERIFIED_ROAD_NETWORK_HTML.md) · [OSM 공개 래스터 타일 정책](https://operations.osmfoundation.org/policies/tiles/)

HTTP 오류, 타일 실패, 네트워크 단절, WebGL 오류 또는 12초 연결 시간 초과 시 온라인 레이어를 해제한다. 이미 받은 일부 타일도 함께 제거하고 자동 재시도를 중단한다. 상태 줄에 확인 가능한 HTTP 상태나 실패 유형을 표시한다. 브라우저나 네트워크가 교차 출처 요청을 차단하는 경우에도 내장 배경으로 전환한다. 사용자가 **배경지도 다시 연결**을 누르면 새로 연결하고, 성공하면 내장 배경을 숨긴다. **배경지도 끄기**로 온라인 배경을 직접 숨길 수 있다.

내장 배경은 이전 검증본의 부여군·청양군 경계와 군명, S0의 주요 도로와 원본 OSM 이름·도로번호를 사용한다. 지명 태그에 대응하는 원본 노드 좌표가 있을 때 추가 지명을 표시한다. 축척과 범례는 계속 표시된다. 확보된 파일에 배포 가능한 독립 수계 도형이 없어, 하천과 수계는 온라인 지도에서 확인한다. 분석 도형의 시나리오별 지연 로딩과 Canvas 렌더링은 배경지도와 독립적이다. 표시용 단순화는 도로 1.5m, 침수 마스크 10m의 EPSG:5186 기준을 적용한다. 분석 집계는 기존 QA와 CSV에서 읽는다.

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

### Windows Edge·Chrome 확인

1. 경량 ZIP을 압축 해제한 뒤 ZIP 내부의 `START_HERE.html`과 빌드 폴더의 동명 파일을 각각 더블클릭한다. 주소창은 `file:///`이며 서버나 브라우저 보안 설정 변경이 필요 없다.
2. 인터넷 연결 상태에서 OpenFreeMap 출처와 배경지도 위의 도로·시설을 확인한다. 확대·이동, 배경 켜기·끄기, 레이어 전환, 시설 검색과 상세를 확인한다. 침수 도형 확인에는 빌드 폴더의 로컬 HTML을 사용한다.
3. 각 브라우저에서 연결을 끊고 다시 연다. 내장 군 경계·주요 도로와 축척·범례를 확인하고, 팝업과 분석 레이어를 계속 사용할 수 있는지 확인한다. 연결을 복구하고 **배경지도 다시 연결**을 눌러 일반 지도가 돌아오는지 확인한다.
4. `SHARE_METADATA.json` 및 대시보드에서 146개 고정 접속점, S0 개방 146, S1 개방 130·차단 16, S2 개방 128·차단 18을 확인한다.

합성 데이터 테스트는 `python -m pytest -q tests/test_build_team_share_package.py`로 실행한다. Playwright가 있는 환경에서 `TEAM_SHARE_CHROMIUM`에 Chromium 실행 파일을 지정하면 외부 요청 차단과 내장 배경을 검증한다. 온라인 렌더링과 오류 주입 테스트에는 공식 npm 패키지 `maplibre-gl@5.24.1`의 `dist/maplibre-gl.js`, `dist/maplibre-gl.css`를 같은 로컬 폴더에 두고 `TEAM_SHARE_MAPLIBRE_ASSETS`에 그 경로를 지정한다. 테스트는 실제 MapLibre로 합성 타일을 렌더링하고 403·429·네트워크 실패·일부 타일 성공을 모의한다. Windows의 공개 서비스 `file://` 확인은 위의 절차로 진행한다.
