# 부여·청양 100년 빈도 국가·지방하천 침수심 통합 및 S0/S1/S2

## 분석 개념

4개 100년 빈도 SHP를 통합한다.

- 부여군 국가하천 `RFM_SGG_NTN_44760_100`
- 부여군 지방하천 `RFM_SGG_RGN_44760_100`
- 청양군 국가하천 `RFM_SGG_NTN_44790_100`
- 청양군 지방하천 `RFM_SGG_RGN_44790_100`

기존 20개 원본 MultiPolygon은 수정하지 않는다. 침수심 분류와 출처는 [공식 API 코드표 기록](FLOOD_SEG_CODE_CODEBOOK.md) 및 [config/flood_seg_code_100y.json](../config/flood_seg_code_100y.json)에 근거한다.

| 코드 | 침수심 | S1 차단대상 | S2 차단대상 |
|---|---|---|---|
| N330 | 0.5m 미만 | 아니오 | 예 |
| N331 | 0.5~1m | 예 | 예 |
| N332 | 1~2m | 예 | 예 |
| N333 | 2~5m | 예 | 예 |
| N334 | 5m 이상 | 예 | 예 |

**중복 공간 처리 원칙:** 국가·지방하천 또는 지역별 자료의 폴리곤이 같은 장소에서 서로 다른 침수심 등급을 표현하면 **가장 깊은 등급(N334 우선)의 공간 범위에 할당**한다. 이는 수리학적 수치해석이 아닌 보수적 연구 시나리오 가정이며, 한 번 중복 제거한 지점은 딱 하나의 등급에 속하도록 한다.

**핵심:** 앞서 실패한 EPSG:5186→5179 또는 →4326 좌표변환을 피하고, **모든 중첩·합집합·면적·GeoPackage 레이어를 공식 SHP 원래 좌표계 EPSG:5186에서 계산**한다. 원본 SHP 유효성 검사를 반복하고 오류 도형을 임의로 수선하지 않는다.

## 실행 방법

기존 `Kor_map-main`에 아래 두 파일이 존재하면 전체 GitHub 저장소를 재다운로드할 필요가 없다.

- `scripts/build_flood_depth_scenarios_100y.py`
- `config/flood_seg_code_100y.json`

PowerShell을 `Kor_map-main` 폴더에서 열고 다음 한 줄을 실행한다.

```powershell
py -m scripts.build_flood_depth_scenarios_100y --input-dir "$env:USERPROFILE\Desktop\flood_100y_raw" --output-dir "$env:USERPROFILE\Desktop\flood_depth_100y_result"
```

로컬에 GeoPandas가 설치되지 않았다면 `py -m pip install geopandas`로 설치한다.

**기존 출력 폴더를 덮어쓰지 않으므로**, 동일한 출력 이름이 이미 있으면 새 이름을 지정해야 한다.

## 출력

바탕화면 `flood_depth_100y_result` 폴더에는 다음 파일들이 생성된다.

| 파일 | 의미 |
|---|---|
| `flood_100y_depth_scenarios.gpkg` | QGIS에서 열 수 있는 다중 레이어 GIS |
| `flood_depth_qa.json` | 코드·면적·중복 및 원본 파일 해시 |
| `flood_depth_qa.md` | 한국어 QA 보고서 |

GeoPackage 주요 레이어:

- `source_categories` — 국가·지방하천 4종 총 20개 원본 클래스 공간 형상과 코드
- `depth_priority_5` — 가장 깊은 등급 우선 중복 제거 후 남은 5개 등급별 면적
- `flood_union_footprint` — 모든 하천 100년 빈도 침수범위를 합친 공간 범위
- `scenario_s1_closure_zone` — N331~N334 차단 후보구역
- `scenario_s2_closure_zone` — N330~N334 차단 후보구역
- S0는 침수로 인한 차단 영역 없음 — 별도 빈 레이어 생성하지 않음

## 검증 및 제한

- 코드별 독립 합집합과 우선순위 결과의 총면적을 검증한다.
- 가장 깊은 등급 우선 이후 5개 구역이 서로 겹치지 않는지 면적 보존 검증.
- S1은 S2의 부분집합이며 S2가 전체 침수구역과 일치해야 한다.
- 보고서의 원본 단순 면적 합은 중복을 포함하므로 실제 침수면적이 아니다.
- 국가/지방하천의 각 100년 빈도 범람지도를 겹친 *합성 위험도 시나리오*이며 하나의 실제 동시 발생 100년 홍수는 아니다.
- 공식 코드표는 국가하천 API 항목에 해당한다. 지방하천 SHP도 같은 5개 코드를 사용하나 의미까지 동일한지는 지방하천 API 자료와 비교하면 더 정확하다.
- 교량/고가 및 도로 높이와 실제 통행 가능 여부는 고려하지 않는다.
- 이번 산출물은 침수 **공간 구역**이다. 차단된 도로 간선 목록 및 수송 경로 계산은 아직 수행하지 않았다.

GitHub Actions 자동 테스트는 가상 SHP로만 실행한다. 원본 SHP를 Actions나 공개 저장소에 올리지 않는다.

**다음 검증에 필요한 파일:** `flood_depth_qa.json`, `flood_depth_qa.md`. 차단 도로 산정에는 이후 `.gpkg`도 필요하다.
