# 2026-11-20 검증 통합 도로망 HTML 뷰어

기존 `data/20261109T045434949410Z/{buyeo,cheongyang}/map.html`과 `output/manual_merge/shelters_roads_integrated.html`은 **2026-11-09 군별 초기 OSMnx Drive 도로망**을 표시하는 과거 기록이므로 덮어쓰지 않습니다.

최신 통합 도로망은 기존 저장된 **Road Profiles 실행 #44992793405**와 **Connected Road Access QA #45010076547**의 성공한 Artifact에서 읽어 `integrated_road_network_20261120.html` 하나를 새로 생성합니다.

## 사용법

1. GitHub Actions → **Verified Integrated Road Network HTML** → **Run workflow**를 선택합니다.
2. 기본 실행 ID와 Artifact 이름을 유지합니다. 모두 **이미 계산된** 자료를 사용하며 전국 PBF를 재처리하지 않습니다.
3. 성공한 실행 하단 Artifacts에서 `verified-road-network-html-...` ZIP을 내려받습니다.
4. 압축을 해제한 뒤 `integrated_road_network_20261120.html` 파일을 더블클릭합니다. Python, 웹 서버 없이 **로컬 브라우저에서 열 수 있습니다. 단 Leaflet JS/CSS와 OpenStreetMap 배경 타일을 내려받으려면 인터넷 연결이 필요**합니다. HTML에는 도로·시설 형상 데이터가 내장돼 있습니다.
5. 우측 레이어 선택기에서 **Drive+Service**(기본), **Drive**, **구호차량 후보망**을 켜거나 끌 수 있습니다. 파랑은 일반도로, 주황은 Service, 초록은 시설, 빨강은 최근접 도로 배정이 QA에서 보정된 시설입니다. 시설 클릭 시 세 모델의 기존 최근접 직선거리와 연결 적격 직선거리를 비교할 수 있습니다.

## 검증과 해석

- 원본 Artifact의 `metadata.json` `data_validated` 상태, 각 단순화 GraphML 파일 SHA-256, QA `qa_metadata.json`의 `qa_data_validated` 상태, `facilities_connected_access_147.csv` 해시·147행·146개 평가·1개 제외, 두 Artifact의 전국 PBF SHA-256 일치를 검사합니다.
- 전체 3개 방향성 GraphML의 원본 형상으로 표시하며 왕복 중복선은 렌더링 과정에서만 제거합니다. 도로 그래프와 시설·QA 원본은 덮어쓰지 않습니다.
- 통계·소스 SHA-256·HTML SHA-256은 함께 제공되는 `viewer_metadata.json`에 보존합니다.
- **보이는 도로에 진입·통행이 가능한지는 현장 검증 전까지 미확정**입니다. 시설 마커는 주소 대표점이며 차량 출입구가 아닙니다. QA의 두 거리는 도로 형상까지의 직선거리이고 실제 차량 주행거리나 최단 경로가 아닙니다.
- 지도에서 도로 및 시설이 많아 보이는 환경에서는 브라우저 초기 로딩·확대·레이어 토글이 느릴 수 있습니다. 좌표는 EPSG:4326입니다.
- 이번 작업은 지도를 생성하는 새 워크플로 추가이며 기존 추출·접근성 분석·침수 분석·수송 최적화는 실행하지 않습니다.

## 로컬에서 HTML 직접 생성

이미 내려받아 압축을 푼 Artifact 폴더가 있다면:

```bash
python -m pip install -r requirements.txt
python -m scripts.render_verified_road_network_html \
  --profile-artifact /path/to/road-profile-comparison-44992793405-1 \
  --qa-artifact /path/to/connected-access-qa-45010076547-1 \
  --output /path/to/new-viewer-folder
```

기존 출력 폴더가 있으면 실수로 덮어쓰지 않도록 실패합니다.
