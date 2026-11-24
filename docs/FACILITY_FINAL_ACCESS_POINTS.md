# 시설 최종 접속점 확정과 시나리오 연결 자료

기존 자동 투영 결과와 30곳의 검토 완료 CSV를 결합한다. 기존 자동 투영점 116곳, 검토 유지 26곳, 지도에서 표시된 실제 입구 기반 보정 4곳을 연구용 접속점으로 기록한다. 보정 4건은 수동 검토에서 확인한 위치 정밀화다. 검토자가 보고한 물리적 접근 불가 사례는 0건이다.

## 입력과 검증

- 기존 분석 실행 폴더의 `road_scenarios.gpkg`, `validation.json`, `relief_vehicle_candidate_s1_edge_trace.csv`, `relief_vehicle_candidate_s2_edge_trace.csv`를 사용한다. S0 레이어를 별도로 내보낸 사본을 사용할 때는 원래 투영 분석에 사용한 그 파일을 `--s0-gpkg`로 지정한다.
- 기존 침수 GeoPackage의 `scenario_s1_closure_zone`과 `scenario_s2_closure_zone`을 읽는다. 각 시나리오의 도로 trace와 침수 GeoPackage 원본을 기존 `validation.json`의 SHA-256 기록과 대조한다. 침수 폴리곤 생성과 도로망 중첩 연산은 반복하지 않는다.
- 이전 투영 분석 결과 중 `facility_projection_distances.csv`, `facility_projection_review_queue.csv`, `facility_projection_audit_sample.csv`, `facility_projection_summary.json`의 위치를 `--projection-dir`로 지정한다. 기존 우선 검토 18곳과 표본 12곳을 그대로 사용한다.
- `source_node_tags.json`, 검증 시설 CSV와 수동 검토 결과 `facility_projection_manual_review.csv`가 필요하다. 입력 시설 ID·주소·좌표, 기존 투영 좌표, 검토 상태와 보정 간선 ID·누적거리를 대조한다. 보정 좌표는 지도 클릭이 같은 S0 적격 도로에 투영되는지와 함께 확인한다. 브라우저 CSV 반올림에 따른 비교 허용오차는 0.002m이다.
- 원본 방향 간선 ID와 `start_m`·`end_m`으로 기존 자식 간선의 분할 및 방향 연결을 확인한다. S1의 차단·검토 구간이 S2에서 재개방되는지 확인한다.

## Windows PowerShell 재현

저장소 루트에서 실행한다. 이전에 받은 `facility_projection_results.zip`을 푼 디렉터리를 `$projection`에 지정한다. 아래 경로는 이전 Windows 실행에서 사용한 구조를 예시로 든 것이므로 다운로드하거나 저장한 실제 경로와 맞춘다. 결과 ZIP에 동봉한 두 파이썬 소스 파일이 현재 체크아웃에 없으면 각각 저장소의 `scripts` 아래에 추가한다.

```powershell
$ErrorActionPreference = 'Stop'
$python = '.\.venv\Scripts\python.exe'
$existing = 'work/flood-road-scenarios-20261124-120849'
$projection = 'work/facility-projection-review/results'
$manual = Join-Path $env:USERPROFILE 'Downloads/facility_projection_manual_review.csv'
$nodeTags = 'local_inputs/road_profile_artifacts/source_node_tags.json'
$facilities = 'local_inputs/shelters_final_147_verified.csv'
$flood = Join-Path ([Environment]::GetFolderPath('Desktop')) 'flood_depth_100y_result/flood_100y_depth_scenarios.gpkg'
$output = "work/final-facility-access-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

& $python -m scripts.finalize_facility_access_points `
    --run $existing `
    --flood-gpkg $flood `
    --node-tags $nodeTags `
    --facilities $facilities `
    --manual-review $manual `
    --projection-dir $projection `
    --output $output
if ($LASTEXITCODE -ne 0) { throw '시설 접속점 확정 또는 검증 실패' }
Get-Content -LiteralPath (Join-Path $output 'facility_access_qa.json') -Encoding UTF8
```

별도 S0 파일과 trace ZIP을 사용할 때는 `$existing`을 두 trace CSV와 `validation.json`이 있는 폴더로 지정하고 명령에 `--s0-gpkg '경로/s0_road_network.gpkg'`를 추가한다. 입력 GeoPackage와 trace는 읽기 전용으로 사용하며 새 결과 폴더가 필요하다.

## 산출물과 후속 활용

| 파일 | 내용 |
|---|---|
| `facility_final_access_points.csv` | 시설 146곳의 대표 좌표, 기존·최종 접속점, 선정 방식, 검토 근거, S0 방향 간선 ID와 위치 |
| `access_point_scenarios.csv` | 438행의 고정 접속 위치와 S0·S1·S2 도로 trace 상태 |
| `facility_connector_flood.csv` | 292행의 시설 대표점·도로 접속점의 마스크 위치와 직선 접속선의 내부 겹침 길이 |
| `access_point_od_connectors.csv` | 기존 방향 자식 간선에서 시설 도착 전용 노드까지의 연결 구조와 부분 길이 |
| `facility_access_qa.json` | 건수, 검증 기준, 입력·출력 SHA-256 |

시설 키는 `county, facility_id`이며 `access_point_id`는 세 시나리오에서 동일하다. 최종 접속점의 5186 좌표는 검증된 방향 간선 누적거리에서 복원하고, 위경도는 수동 검토 또는 기존 자동 분석이 저장한 값을 보존한다.

도로 trace의 `open`은 해당 도로 위치 주변이 시나리오에서 열려 있다는 뜻이다. `blocked`는 그 위치가 차단된 자식 간선에 속하거나 차단 분할 경계에 수치 허용오차 0.000001m 이내로 닿는 경우다. `review`는 교량·고가 검토 상태를 뜻한다. `review` 간선을 통행 가능으로 보는 경우와 닫는 경우의 링크 사용 여부를 별도 열에 둔다. 상태가 나빠져도 접속점은 S0에서 정한 위치 그대로 유지한다.

후속 행선지 모델에서는 허용된 행의 `arrival_node_id → arrival_sink_id`를 원래 시나리오 그래프에 도착 전용으로 붙인다. 각 방향의 `arrival_child_id`와 원래 간선 기준 `partial_start_m`, `partial_end_m`을 보존한다. 자식 간선 내부에서는 상류 노드부터 접속점까지의 부분 길이가 `arrival_length_m`이며, 기존 노드 또는 허용된 분할 노드에 정확히 닿으면 0m다. `arrival_sink_id`에서 나가는 간선을 생성하면 같은 시설을 경유해 역주행하거나 다른 도로로 넘어가는 가상 경로가 생길 수 있다. 시나리오별 분할 노드 ID는 해당 시나리오 안에서만 사용한다. 실제 코스트와 도로망 OD는 후속 단계에서 정의한다.

시설 대표점, 직선 접속선, 도로 접속 위치는 서로 다른 공간 검토 대상이다. 점은 폴리곤 내부, 경계, 외부로 구분한다. 선은 내부 겹침 길이와 경계에만 접촉하는 경우를 구분한다. 시설–도로 직선은 실제 진입로 위치나 고도를 나타내지 않는다. 지도상 입구 정밀 보정과 기존 투영점 유지 모두 연구용 위치 결정이며, 실제 차량 진입 확인 상태는 `unverified`다. 도로의 열림 상태는 특정 창고에서의 도달 가능성을 보장하지 않는다.
