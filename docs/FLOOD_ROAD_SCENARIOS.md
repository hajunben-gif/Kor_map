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

## 실행

아래 명령은 저장소 루트에서 실행한다. Windows에서는 `python` 대신 `py`를 사용할 수 있다. 출력 경로에는 새 폴더를 지정한다.

```sh
python -m pip install -r requirements.txt -r requirements-pbf-audit.txt
python -m scripts.build_flood_road_scenarios --validate-flood-only --output work/flood-input-check
python -m scripts.build_flood_road_scenarios --output work/flood-road-scenarios
python -m unittest discover -s tests -p test_build_flood_road_scenarios.py
```

입력 위치는 `--flood`, `--flood-qa`, `--road-artifact`, `--facilities`로 지정할 수 있다. 진행 로그는 레이어 검증, 프로필 처리량, 시나리오 내보내기를 표시한다.

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
