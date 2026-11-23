# 부여·청양 100년 빈도 범람구역 통합 (침수심 코드 해석 전)

## 목적

사용자가 확보한 4개 검증 SHP (국가하천/지방하천 × 부여/청양, 모두 100년 빈도)를 **중복 제거된 하나의 범람구역 레이어**로 통합한다. 원본의 다섯 `SEG_CODE`는 그대로 유지한다. 공식 코드표를 확인하지 않았으므로 수심(m)이나 차량 통행 제한 등급은 부여하지 않는다.

## 로컬 실행 — 원본 업로드 불필요

이전에 압축 해제했던 바탕화면 `flood_100y_raw` 폴더의 20개 원본 파일을 그대로 사용한다.

PowerShell을 GitHub `Kor_map-main` 폴더에서 열고 다음 두 줄을 순서대로 실행:

```powershell
py -m pip install geopandas
py -m scripts.integrate_flood_100y --input-dir "$env:USERPROFILE\Desktop\flood_100y_raw" --output-dir "$env:USERPROFILE\Desktop\flood_100y_integrated"
```

**주의:** 같은 이름의 출력 폴더가 이미 있으면 덮어쓰지 않고 중단한다. 재실행 시 출력 폴더 이름을 바꾼다.

출력 폴더:

- `flood_100y_integrated.gpkg` — QGIS에서 직접 열 수 있는 GIS 결과물
  - `source_categories`: 20개 원본 범람 폴리곤; `SEG_CODE`, 국가/지방하천 유형, 출처 및 행정구역 보존
  - `union_footprint`: 네 원본을 기하학적으로 합친 **전체 범람구역**; 수심 정보 없음
- `integration_qa.json` — 원본별 파일 SHA256, 단순면적, 합집합 면적, 각 원본 쌍의 중복 면적
- `integration_qa.md` — 같은 내용을 사람이 확인하기 쉬운 보고서로 정리

모든 GIS 산출물은 EPSG:5179로 재투영한다. 원본 SHP는 수정하지 않으며 이 ZIP이나 원본 SHP를 공개 저장소에 올리지 않는다.

## 해석과 제한사항

- 기존 지역별 국가·지방하천 100년 빈도 SHP 모두 타당한 지형정보라는 점은 먼저 로컬 GeoPandas 검증 결과로 확인했다.
- 공식 수심 **분류 범례**는 5개(0.5m 이하 / 0.5~1m / 1~2m / 2~5m / 5m 이상)이지만, **`SEG_CODE` 값 N330~N334가 어느 구간에 해당하는지는 검증된 공식 코드표를 아직 확보하지 못했다.** 숫자 오름차순으로 대응시켜 계산하면 안 된다.
- 이 스크립트는 중첩 부분의 수심을 결정하지 않으며, `source_categories`의 20개 피처를 합치지 않고 보존한다.
- `union_footprint`는 국가·지방하천 100년 빈도 **개별 위험도의 기하학적 합집합**이다. 하나의 실제 동시 발생 100년 홍수 사건이나 실제 차량 통행 제한을 나타내지 않는다.
- 원본별 수심 등급 폴리곤이 서로 겹치면 원본 피처 면적 합산이 실제 단일 침수면적보다 클 수 있다.
- 보고서의 국가·지방, 시군 쌍별 중첩 면적을 전부 합산하면 다중중복이 생기므로 전체 중복 면적과 같다고 볼 수 없다.
- 도로망·교량 중첩은 다음 단계에 별도로 수행한다.

## 테스트

```powershell
py -m unittest discover -s tests -p test_integrate_flood_100y.py -v
```

CI 테스트는 임의로 생성한 도형으로만 수행한다. **실제 GIS 통합 결과가 나왔다고 주장하려면 로컬 원본으로 직접 실행하고 `integration_qa.json`과 `integration_qa.md`를 확인해야 한다.**

이후 실데이터 QA 결과를 확인하고 공식 `SEG_CODE` 코드표를 확보한 뒤 수심 등급 통합과 S0/S1/S2 통행 제한 가정을 확정한다.
