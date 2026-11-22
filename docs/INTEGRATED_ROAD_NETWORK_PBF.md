# 통합 2km 도로망 PBF 분석

이 워크플로는 Geofabrik의 대한민국 PBF에서 부여군·청양군 통합 2km 도로망을 추출하고, 좌표가 확보된 시설 146곳 및 기존 이상치 6곳의 거리를 재검증합니다.

## GitHub Actions에서 실행

1. PR 검토 후 워크플로 파일을 기본 브랜치 `main`에 반영합니다. GitHub에서 새 수동 워크플로를 표시하려면 파일이 기본 브랜치에 있어야 합니다.
2. **Actions → Integrated Road Network (PBF) → Run workflow**를 열고 `main`을 선택합니다. [실행 화면](https://github.com/hajunben-gif/Kor_map/actions/workflows/integrated-road-network-pbf.yml)
3. `allow_latest_if_unavailable`을 기본값인 **해제**로 두면 2026-11-09 원본만 시도합니다. 과거 원본을 구할 수 없을 때 최신본 분석을 허용하려면 **체크**한 뒤 실행합니다. 체크한 경우에도 과거 원본을 먼저 시도합니다.
4. 완료 후 실행 페이지의 **Summary**에서 스냅샷 시각·해시·누락 검증 건수를 확인하고, **Artifacts → integrated-road-network-pbf-…**를 내려받습니다. 기본 보존 기간은 14일입니다. 실패한 실행은 해당 단계 로그에서 원인을 확인합니다.

## PBF 확인

- 과거 후보는 공식 경로의 `south-korea-261109.osm.pbf`입니다. 보존 감사에 기록된 SHA-256, 크기 291,406,356바이트, 헤더 시각 `2026-11-09T21:22:17Z`가 일치해야 사용합니다. [보존 감사 입력](../output/road_network_audit/jukrim/verified_20261112/pbf_results/evidence.json)
- 일시적인 연결 오류·시간 초과·서버 오류는 같은 URL에서 최대 세 번 시도합니다. 사용자가 최신본을 허용한 경우, 과거 후보가 HTTP 403·404·410으로 응답하거나 재시도 후 다운로드가 불가능하면 공식 최신 PBF로 넘어갑니다. 과거 후보의 해시·크기·헤더 검증에 실패하면 실행을 중단합니다.
- 최신 파일은 공식 MD5와 대조하고 SHA-256과 PBF 기준 시각을 별도로 기록합니다. `metadata.json`의 `source.download_metadata`에 요청 URL·응답 URL·선택 모드·검증 방식·시작·완료 시각이 남습니다. 현재 최신본 사용 시 `latest_fallback`을 확인할 수 있습니다.
- 전국 PBF는 러너의 임시 파일입니다. 산출물에는 그래프·요약·비교 결과가 포함됩니다.

## 산출물에서 확인할 항목

| 파일 | 확인 항목 |
|---|---|
| `metadata.json` | PBF 출처·기준 시각·SHA-256, 입력·산출물 해시, 그래프 통계, 바깥쪽 공유 노드와 제외된 외부 way ID |
| `way_audit.csv` | 범위와 교차한 원본 highway way별 원본 구간 수, 기대·원본 그래프·최종 그래프의 방향 구간 수, 필터 제외 사유, 군 사이 경계 및 외곽 교차 표시 |
| `raw.graphml`, `simplified.graphml`, `network.gpkg` | 원본 노드·way 식별자와 방향 및 형상 |
| `facilities_comparison_147.csv`, `outliers_existing_6.csv`, `outliers_newly_detected.csv` | 시설별 비교와 이상치 결과 |
| `report.md`, `jukrim.json` | 결과 요약과 죽림리 진입도로·교량·지천로 연결 검증 |

통합 2km 폴리곤에 닿는 원본 way의 전체 선형을 유지합니다. 검증 과정에서 PBF의 원래 방향 구간 ID·노드·형상·태그를 단순화 전후 그래프와 대조하고, 파일 재로딩을 확인합니다. 원본 구간 누락·형상 변경·검증 실패가 있으면 결과 묶음을 게시하지 않습니다.

## 로컬 재현

```sh
python -m pip install -r requirements.txt -r requirements-pbf-audit.txt pytest
python -m pytest -q
python -m scripts.build_integrated_road_network \
  --download-historical --output output/integrated_road_network/20261109_snapshot
```

과거 원본을 확보한 환경에서는 `--download-historical` 자리에 `--pbf /path/to/south-korea-261109.osm.pbf`를 지정합니다. 최신본 대체를 허용할 때는 다운로드 명령에 `--allow-latest-if-unavailable`을 추가합니다. 출력 경로는 실행마다 새로 지정합니다.

## Drive・Service・구호차량 후보 비교 (별도 워크플로)

기존 `Integrated Road Network (PBF)` 및 그 결과는 유지합니다. 새 워크플로는 **Road Profiles (Drive and Service)** (`road-profile-comparison.yml`)이며 `workflow_dispatch`만 사용합니다. PR 병합 후 **Actions → Road Profiles (Drive and Service) → Run workflow → main**에서 수동 실행합니다. 새 워크플로가 아직 기본 브랜치에 없으면 GitHub 수동 실행 목록에 나타나지 않을 수 있습니다. 이 PR 작성 단계에서는 전체 분석이나 워크플로 실행을 하지 않았습니다.

### 한 PBF에서 세 모델

1. 이전 실행 #44920912032의 공식 날짜별 후보 `south-korea-261120.osm.pbf`를 한 번 요청합니다. 사용자 제공 SHA-256 `492ceebb946500fa54dd1b460b4ff923884dd9e673f65d3750e1d60122b85a8e`와 헤더 시각 `2026-11-20T21:21:21Z`를 대조합니다.
2. 파일 부재/접속 불가일 때만 공식 최신 PBF를 한 번 다운로드하고 공식 MD5·헤더와 SHA-256을 검증합니다. 날짜별 후보의 해시·시각 불일치에는 최신본으로 넘어가지 않습니다. 새 비교 워크플로에는 자동 재시도가 없습니다. 기존 단일 Drive 워크플로의 재시도 정책은 유지합니다.
3. **PBF 하나를 한 번 공간 추출한 동일 원본**으로 Drive 기준망, `drive_service` 확장망, 제한 검토를 반영한 후보망을 각각 재구축합니다. 기존 GraphML에 서비스 도로를 덧붙이지 않습니다. 이전 실행의 전국 PBF·결과를 덮어쓰지 않습니다.

이전 실행의 노드 14,390·방향 간선 32,085 및 4곳의 거리 값은 사용자 제공 이력으로 메타데이터에 분리합니다. 새 모델 간 통계는 현재 한 PBF의 실제 계산값입니다. SHA-256이 다르면 이전 실행과의 차이를 service 효과만으로 해석하지 않습니다. 과거 Artifact 목록은 확인할 수 있어도 그 사실만으로 Artifact 내부 수치를 독립 검증했다고 간주하지 않습니다.

### 후보 정책과 통행 미확인

- OSMnx 2.1.1의 정확한 두 필터를 메타데이터와 각 도로 감사표에 기록합니다. `drive_service`도 `service=parking`, `parking_aisle`, `emergency_access`, `private`, `access=private` 등을 제외합니다. 탈락한 highway도 원본 감사 JSONL에 남습니다.
- 일반 자동차 접근권은 `motor_vehicle → vehicle → access` 우선순위로 읽고, `motorcar`·`hgv` 제한도 보수적으로 검토합니다. `no/private/permit/customers/agricultural/forestry` 등 명시적 제한과 `smoothness=impassable`은 후보에서 제외합니다. 조건부 예외가 있어도 자동 허용하지 않고 감사 기록을 남깁니다. `track`·보행로 등은 기본 후보에 편입하지 않습니다.
- 목적 제한, 조건부 규칙, 차량별 일방통행 예외, 폭·높이·중량, 노면, 교량, 원본 노드의 barrier는 검토 사유로 기록합니다. 구호차량의 제원·진입권·현장 상태를 확인하지 않았으므로 **모든 후보에 review_required=true, vehicle_passage_confirmed=false**를 기록합니다. 태그 누락도 통행 승인 근거가 아닙니다.
- 회전 제한 relation과 노드 태그는 보존하지만 라우팅 제약으로 자동 해석하지 않습니다. 후보 GraphML은 통행을 확정한 경로 계산용 그래프가 아닙니다.

### 시설과 도로 비교

시설 식별자는 `(시군명, 시설일련번호)`입니다. 동일 원본 147행의 기존 열을 보존하고, 146개 좌표 보유 시설에 세 모델의 최근접 직선거리·WCC·SCC를 기록합니다. 좌표 없는 대동마을회관 1개는 제외 상태를 유지합니다. 50·100·300m 초과 건수, 거리 증감 및 기존 6곳 판정을 비교합니다.

개곡리 125·지곡리 160·덕성1리 150·천장리 145는 가장 가까운 원본 service way의 전체 태그·원본 형상·노드 순서·필터 탈락 사유·방향별 연결성을 별도 기록합니다. 연결은 실제 공유 OSM 노드만 사용하며, 가까운 선의 공간적 교차로 접속점을 생성하지 않습니다. 일반 기준망 연결은 **Drive 최대 WCC의 motorway/trunk를 제외한 일반도로 노드**로 진입·이탈 가능한지를 검사합니다. 외곽 범위 밖 연결, 회전 제한, barrier, 실제 시설 진입점은 여전히 미확인입니다. `motorway`·`trunk`와 링크가 최근접이면 별도 표시하며 직선거리 감소를 차량 접근 개선으로 확정하지 않습니다. 상세 HTML 4개는 배경 타일 없이 원본 선형·시설을 표시합니다. Leaflet 라이브러리를 불러오려면 인터넷이 필요합니다.

### Artifact와 식별자

완전히 검증된 실행만 `road-profile-comparison-<run_id>-<attempt>` Artifact를 게시하며 14일 보존합니다. 실패하면 최종 결과 폴더를 게시하지 않고 Actions 로그에 원인을 표시합니다. 프로세스 종료 성공과 `metadata.json`의 `status=data_validated`는 데이터 검증 상태이며 실제 차량 통행 검증과 다릅니다.

- `drive_reference.graphml`, `drive_service.graphml`, `relief_vehicle_candidate.graphml`
- 각 profile 하위 폴더의 `raw.graphml`·`simplified.graphml`
- `road_network_profiles.gpkg`: 세 모델 × 원본/단순화 × nodes/edges 레이어
- `road_profile_comparison.csv`, `facilities_access_comparison_147.csv`
- `service_road_audit.csv`, `remaining_outliers_4.csv`, `service_detail_*.html`
- `source_segment_correspondence.csv`, `source_road_audit.jsonl`, `source_node_tags.json`, `restriction_relations.json`
- `jukrim_profiles.json`, `metadata.json`, `report.md`

원본 방향 구간 ID는 `PBF SHA-256 + way ID + 원본 구간 순번 + from/to 노드`로 결정합니다. 단순화 간선의 `edge_uid`/`parent_edge_id`는 순서 있는 원본 방향 구간 ID들로 결정합니다. `source_node_ids`, `source_segment_ids`, `osm_tags`, `length_5179`를 함께 저장하므로 서로 다르게 단순화된 모델도 원본 구간 ID로 대응합니다. 향후 침수 분할 시 새 자식 ID와 이 부모 ID를 함께 유지해야 합니다. 이번 구현은 침수자료 확보·분할·최단경로·최적화를 하지 않습니다.

로컬 PBF가 이미 있는 환경의 실행 예:

```sh
python -m scripts.compare_road_profiles \
  --pbf /absolute/path/south-korea-261120.osm.pbf \
  --sha256 492ceebb946500fa54dd1b460b4ff923884dd9e673f65d3750e1d60122b85a8e \
  --source https://download.geofabrik.de/asia/south-korea-261120.osm.pbf \
  --output /new/external/path/road-profiles
```

전국 PBF·위치 인덱스는 임시 디렉터리 밖으로 배포하지 않으며 Git이나 Artifact에 포함하지 않습니다. 새 `--output`은 저장소 외부의 존재하지 않는 경로여야 합니다. 고정 PBF를 사용하지 않는 `--download` 실행은 실행 시점마다 데이터가 달라질 수 있습니다.
