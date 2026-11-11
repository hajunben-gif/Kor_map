# 죽림리마을회관 PBF 조사 수동 실행

GitHub 저장소의 **Actions → Jukrim Geofabrik PBF Audit → Run workflow → main → Run workflow**에서 실행합니다. `workflow_dispatch`만 정의되어 있으므로 push·PR·예약 실행으로 시작되지 않습니다. API 키는 필요하지 않습니다.

공식 출처는 https://download.geofabrik.de/asia/south-korea-latest.osm.pbf 입니다. 공식 MD5와 대조하고 SHA-256, 다운로드 시각, HTTP Last-Modified, PBF 헤더의 `osmosis_replication_timestamp`를 기록합니다. 데이터 기준 시각은 다운로드 시각과 구분합니다. 다운로드·체크섬·기준 시각 확인에 실패하면 중단하며 대체 서버나 Overpass를 사용하지 않습니다. 최신 파일과 MD5가 갱신 중 서로 달라지는 경우에도 중단합니다.

전국 PBF는 runner 임시 디렉터리에만 저장합니다. 시설 중심 2km 원을 여유 있게 포함하는 사각형을 `osmium extract -s complete_ways`로 추출하고, way가 참조하는 모든 노드를 `osmium check-refs`로 확인합니다. 사각형 추출은 노드 포함 기준이며, 사각형 안에 노드가 전혀 없는 매우 긴 통과 way는 제외될 수 있습니다. 추출 뒤 EPSG:5179에서 원본 way 형상까지 거리가 2,000m 이하인 도로만 분석합니다. 경계에서 형상을 자르거나 가상 연결선을 만들지 않습니다.

기존 `output/road_network_audit/jukrim/audit.py`의 **오프라인 함수만** 재사용합니다. 해당 모듈의 fetch/main 함수는 호출하지 않습니다. OSMnx 2.1.1의 drive·drive_service 필터를 같은 PBF 스냅샷에 적용하고, 범용 highway 결과와 비교합니다. 두 군 GraphML의 원래 way ID 집합으로 포함 여부를 확인합니다. 기존 CSV와 이전 조사에서 1,359.55m 및 259.89m를 읽고 보존합니다. 기존 군별 범위 차이와 현재 태그 필터 제외 이유를 구분하며, 과거 태그와 현재 태그가 같았다고 가정하지 않습니다.

실행이 성공하면 해당 실행의 **Artifacts → jukrim-pbf-audit-…**에서 다음 파일을 받습니다.

- `roads.csv`: 반경 내 way ID·거리·주요 태그·전체 태그·기존 두 군 그래프 포함 여부·필터 제외 조항·차량 통행 평가.
- `comparison.csv`: 기존 두 조건과 PBF drive·drive_service·범용 도로 최근접 거리 비교.
- `evidence.json`: 출처·체크섬·데이터 기준 시각·필터·원본 way 형상 및 태그·기존 경계 근거·보존 검증.
- `report.md`: 거리 차이와 원인, 미확인 사항 요약.

PBF·GraphML·중간 원자료는 Artifact에 포함하지 않습니다. 결과는 저장소에 자동 커밋하지 않습니다. 실패한 실행은 성공 결과 Artifact를 만들지 않으므로 실패 단계의 Actions 로그를 확인합니다. Artifact 보존 기간은 14일입니다.

OSM 도로 존재와 필터 통과는 차량 통행 가능성의 증명이 아닙니다. 통행권·현장 상태·교량 하중·차량 제원·조건부 제한은 미확인으로 남깁니다. 전체 도로망 재구축, 다른 시설 조사 또는 구호 수송망 자동 편입은 하지 않습니다. 원본 데이터의 기준 시각·범위가 달라 과거 누락의 모든 원인을 확정할 수는 없습니다.

개발 검증:

```sh
python -m pip install -r requirements.txt -r requirements-pbf-audit.txt
python -m unittest discover -s tests -p test_jukrim_pbf_audit.py -v
```

테스트는 명시적인 합성 fixture만 사용하며 실제 조사 결과가 아닙니다. 네트워크 차단 상태의 PBF 해독·필터·CSV/JSON/보고서 생성, 누락 노드 실패 처리, 공식 다운로드 실패 시 중단, 추출 전략을 검증합니다. 실제 대한민국 PBF 다운로드 및 Actions 실행은 사용자의 수동 실행 때 수행합니다.
