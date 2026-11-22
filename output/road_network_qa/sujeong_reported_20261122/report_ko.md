# 수정초등학교 강당 고립 Service 도로 QA — 입력 관측과 검증 한계

이 문서는 **사용자가 제공한 관측과 코드 검토 결과**입니다. 원본 Artifact를 읽어 재계산한 최종 146개 시설 결과가 아닙니다. 새 거리를 추정하지 않았습니다.

대상은 청양군 시설일련번호 18, 수정초등학교 강당입니다. 제공된 Drive 최근접 직선거리는 82.462631m, Drive+Service 최근접 직선거리는 72.665399m입니다. 제공된 최근접 service way는 1228606370이며 integrated_wcc_082, 일반도로망 진입·복귀 모두 False입니다. 같은 내용은 reported_observations.csv에 출처 상태를 명시해 보존했습니다. 구호차량 후보 거리와 양방향 연결 적격 거리는 미계산으로 비워 두었습니다.

## 확인한 배정 로직의 문제

기존 scripts/compare_road_profiles.py는 전체 그래프에 base.compare_facilities를 적용해 기하학적 최근접 간선을 먼저 선택합니다. 이후 anchor_reachability 결과로 can_reach_ordinary·reachable_from_ordinary를 표시하지만, 그 값이 False여도 최근접 거리와 배정을 바꾸지 않습니다. 따라서 고립 도로가 가까우면 작은 직선거리와 연결 불가 상태가 동시에 기록됩니다. 기존 수치를 덮어쓸 사유가 아니라 두 지표를 구분할 사유입니다.

이 코드 동작은 확인했지만, 해당 way의 고립을 만든 원본 OSM 원인 자체는 아직 확인하지 못했습니다. 노드 미공유, 필터 제외된 연결도로, 외곽 범위, 일방통행 또는 국소 도로망 전체의 분리를 구분하려면 원본 지역 감사 자료와 raw 그래프가 필요합니다. 물리적 도로가 실제로 없거나 학교 출입이 불가능하다고 단정하지 않습니다.

## 실제 자료 접근 상태

성공 실행 44992793405의 Artifact road-profile-comparison-44992793405-1을 GitHub에서 확인했습니다. ID는 14742996621이며 압축 크기는 481,835,106바이트입니다. 실행 환경으로 전송하는 HTTPS 요청은 프록시 403 Forbidden으로 차단돼 내용을 읽지 못했습니다. Artifact 목록 확인이나 다운로드 참조 생성만으로 원본 노드를 검증했다고 간주하지 않습니다.

전국 PBF 다운로드·처리와 실제 분석 워크플로 재실행은 하지 않았습니다. 기존 세 GraphML, GeoPackage, 시설 CSV 및 과거 결과는 변경하지 않았습니다.

## 추가한 QA의 계산 기준

- 기존 단순 최근접 거리를 유지하고, 일반 기준망에 진입·복귀 가능한 방향 간선까지의 최근접 직선거리를 별도로 계산합니다.
- Drive 최대 WCC 내 일반도로 노드가 가장 많은 SCC를 고정 기준으로 정합니다. 각 profile에서는 그 anchor와 가장 많이 겹치는 단일 SCC를 사용하므로, 후보망에서 기준 노드가 여러 SCC로 갈라져도 서로 다른 기준점으로 진입·이탈한 것을 왕복 연결로 오인하지 않습니다.
- 방향 간선 u→v는 같은 기준 SCC에서 u로 도달하고 v에서 기준 SCC로 돌아올 수 있어야 합니다. 직접 역방향 간선이 없어도 실제 OSM 노드의 우회 복귀 고리가 있으면 적격일 수 있습니다. motorway/trunk 및 링크는 시설 최근접 배정 대상에서 제외합니다.
- 모든 동률 최근접 간선의 연결 상태를 함께 확인합니다. 연결된 동률 대안과 모든 최근접 간선이 고립된 경우를 구분합니다.
- 적격 간선이 없으면 빈 거리와 no_eligible_edge를 기록합니다. 저장 영역 밖에 더 가까운 적격 도로가 있을 가능성은 connected_search_covered로 별도 표시합니다.
- 회전 제한·barrier·조건부 허가·차량 제원·실제 시설 출입구는 해결하지 않았습니다. OSM 양방향 연결성은 차량 통행 승인이 아닙니다.

## 실제 146개 시설 재검증 방법

PR 병합 후 Actions → Connected Road Access QA에서 기본 source_run_id 44992793405와 Artifact 이름을 지정해 수동 실행합니다. 이 작업은 기존 Artifact를 읽고 새 QA Artifact만 만들며 전국 PBF를 다시 처리하지 않습니다. 해당 Artifact가 만료됐거나 파일 해시가 다르면 명확히 실패합니다.

실제 자료를 읽는 실행이 완료되면 facilities_connected_access_147.csv, nearest_assignment_alerts.csv, edge_connected_eligibility.csv, sujeong_way_nodes.csv, sujeong_way_diagnosis.json, qa_metadata.json, qa_report_ko.md가 새 결과 폴더에 생성됩니다. 146개 시설의 두 거리와 수정초등학교 강당 원본 노드·공유 way·방향성 조사 결과는 이 실행 후에 보고할 수 있습니다.
