# 부여군·청양군 자동차 도로망

[실제 데이터·지도·코드 ZIP 다운로드](https://github.com/hajunben-gif/Kor_map/raw/refs/heads/main/downloads/korea-road-network.zip) · [추출 결과](RESULTS.md)

실제 데이터는 `data/20261109T045434949410Z/`에 있습니다. 저장소의 **Code → Download ZIP**으로도 전체를 받을 수 있습니다.


OSMnx 2.1.1로 실제 OpenStreetMap 도로만 다운로드하는 GIS 기초 데이터 프로젝트입니다. 침수도로 또는 가상 도로를 만들지 않습니다. 인터넷 연결이 필요합니다.

## 실행

Python 3.11 이상을 권장합니다. Python 3.12 / OSMnx 2.1.1에서 확인했습니다.

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python extract.py
```

한 지역만 실행하거나 저장 위치와 API 대기 시간을 변경할 수 있습니다.

```bash
python extract.py --regions buyeo --output data --timeout 180 --attempts 3
```

주요 라이브러리: OSMnx, NetworkX, GeoPandas, Shapely, pyogrio, Folium, requests. OSMnx와 Folium의 의존성으로 함께 설치됩니다. 실행 환경의 정확한 버전은 `validated-environment.txt`에 기록했습니다.

## 결과

매 실행은 `data/<UTC 실행시각>/`에 새로 저장됩니다. 각 `buyeo/`, `cheongyang/`에는 다음 파일이 생성됩니다.

- `network.graphml`: 방향성 NetworkX MultiDiGraph. 원래 노드 ID와 `(u, v, key)` 유지.
- `network.gpkg`: `nodes`, `edges` 레이어. 방향별 간선 및 `u, v, key` 보존.
- `boundary.geojson`: 실제 추출에 사용한 경계.
- `metadata.json`: 추출 시각, 경계 출처, CRS, API, 설정, 통계.
- `map.html`: 도로 속성 툴팁과 행정경계가 있는 지도. 배경 타일과 JS/CSS는 인터넷 필요.

`run_status.json`은 지역별 실패를 기록합니다. 하나가 실패해도 다음 지역을 처리하고, 실패가 있으면 종료 코드 1을 반환합니다. 부분 출력 폴더는 실패 결과일 수 있으므로 상태 파일을 확인하세요. 로그의 API URL에는 공개 OSM 질의가 포함됩니다.

## 추출 기준과 한계

- 출처: OpenStreetMap contributors, https://www.openstreetmap.org/copyright (ODbL 1.0). 지도에도 OSM 저작자 표시가 포함됩니다.
- `network_type="drive"`, `simplify=True`, `retain_all=True`. 도로 방향, 평행 간선, 단절된 작은 연결요소를 유지합니다. 단순화로 중간 형상 노드는 geometry로 합쳐집니다. 모든 노드가 실제 교차로인 것은 아니며 고가도로의 평면 교차는 연결로 만들지 않습니다.
- `length`는 OSMnx가 단순화 전 도로 구간 길이를 합산한 미터 값입니다. `highway`, `oneway`, `osmid`, `name` 및 존재하는 기타 속성을 저장합니다. OSM에 없는 값은 임의 보충하지 않으며 단순화된 속성은 리스트일 수 있습니다.
- GraphML/GeoPackage/경계는 EPSG:4326 (경도·위도)입니다. 면적·버퍼 등 미터 단위 GIS 연산은 `to_crs(5179)`로 투영하세요. 경위도 geometry.length를 미터로 해석하면 안 됩니다.
- 출력 통계: 노드 수, 방향별 간선 수, 방향별 길이 합계, 양방향 중복을 제거한 OSMnx 무방향 그래프 길이, 약연결요소 및 강연결요소 수. 평행 도로는 별도 구간입니다. 양방향 길이 합계는 물리적 도로 길이보다 큽니다.
- `truncate_by_edge=False`: 경계 밖 노드를 제외하므로 경계를 가로지르는 일부 구간이 빠질 수 있습니다. 두 군의 그래프는 별도 분석용이며 군 사이 수송 경로는 이 두 그래프를 단순 결합하여 완전하다고 볼 수 없습니다. 추후 두 경계의 합집합과 적절한 외곽 버퍼로 통합 그래프를 별도 추출하는 것이 좋습니다.
- 기록된 시각은 다운로드/실행 시각이며 OSM의 완전한 역사 스냅샷 기준일이 아닙니다. 캐시 재사용 시 원자료가 더 오래되었을 수 있습니다. `data/cache`의 응답을 보존하면 재현에 도움이 됩니다. 최신 재조회가 필요하면 캐시를 다른 위치로 이동하고 실행하세요. Nominatim과 Overpass 질의는 원자적인 동일시점 조회가 아닙니다.
- OSM의 자동차 통행 가능 태그 기준입니다. 현장 통행, 계절 통제, 누락된 제한, 차량 중량·높이·폭, 회전 제한은 보장하지 않습니다. 기본 MultiDiGraph만으로 회전 제한은 구현되지 않습니다. 구호 수송 적용 전에 현장·공식 자료 검증이 필요합니다.

## 행정경계 보완

Nominatim에 충남의 해당 군을 구조화 질의하고 행정경계 relation, 이름, 대략적인 좌표 범위를 확인합니다. 자동 결과는 `boundary.geojson`으로 확인하세요. 경계가 없거나 다르면 공신력 있는 GIS에서 받은 정확한 군 경계를 준비합니다.

```text
boundaries/buyeo.geojson
boundaries/cheongyang.geojson
```

각 파일에는 해당 군만 포함한 유효한 Polygon/MultiPolygon과 CRS가 있어야 합니다. 여러 피처는 합집합으로 처리하며 EPSG:4326으로 변환합니다. 파일이 있으면 자동 검색보다 우선합니다. 원본 제공기관·기준일·라이선스는 `boundaries/SOURCE.md`에 별도로 기록하세요. 파일 SHA-256은 메타데이터에 자동 저장됩니다. 임의 사각형 경계로 대체하지 않습니다.

## 오류와 API 제한

API 연결 오류, 빈 응답, HTTP 오류는 기본 최대 3회, 15/30초 지수 대기로 재시도합니다. 예외 응답에 숫자형 Retry-After가 있으면 적용합니다. OSMnx의 Overpass 슬롯 대기와 캐시도 활성화합니다. OSMnx 내부에서도 일부 상태에 대해 자체 재시도가 있어 전체 대기 시간은 `--timeout`보다 길 수 있습니다. `--timeout`은 요청별 시간입니다. 지속 실패 시 로그를 확인하고 시간을 두고 재실행하거나 정확한 경계 파일을 제공하세요. 여러 프로세스로 공용 API를 병렬 호출하지 마세요. 기본 API는 공개 Nominatim과 Overpass이며 서비스 정책을 준수해야 합니다.

## 향후 대피소·창고와 침수자료 연결

```python
import osmnx as ox
G = ox.load_graphml('data/<실행시각>/buyeo/network.graphml')
# X=경도, Y=위도. 실제 시설 좌표로 별도 시설 테이블을 구성하세요.
# node_id, distance_m = ox.distance.nearest_nodes(G, X=longitude, Y=latitude, return_dist=True)
```

시설은 별도 `facility_id, type, longitude, latitude, nearest_node_id, distance_m` 테이블에 보관하고 연결 거리를 검토하세요. 노드에 강제 병합하면 하천·고가도로 반대편으로 잘못 연결될 수 있습니다. 도로 중간 연결이 필요하면 간선 분할과 역방향 간선을 함께 처리해야 합니다. 원본 그래프는 유지하고 시나리오별 복사본에 실제 확인된 침수 간선 `(u, v, key)`만 차단하세요. 이 프로젝트에는 침수 판정이나 가정 차단 간선이 없습니다.

## 결과 검증

```bash
python validate.py data/<실행시각>
python -m pip install pytest
python -m pytest -q tests
```

검증 스크립트는 GraphML 재로딩, GeoPackage CRS, 노드 ID와 방향별 `(u,v,key)` 집합의 일치, 길이와 HTML 파일을 확인합니다. 테스트에는 가상 도로망을 사용하지 않으며 API 오류와 잘못된 경계 처리만 검사합니다.
