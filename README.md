# 부여군·청양군 긴급구호 수송 네트워크 기초자료

집중호우 침수위험을 고려한 긴급구호물자 비축 및 수송 네트워크 최적화를 목표로 합니다. **현재는 시설 좌표와 도로망 데이터의 신뢰성을 확보하는 단계**이며, 침수위험 분석·Dijkstra 최단경로 계산·CFLP/MILP 최적화는 향후 작업입니다.

## 최신 통합 도로망 및 팀 공유 지도 — 2026-11-22

**도로망 구축, 시설 연결성 QA, 실제 배경지도 포함 HTML 시각화가 완료되었습니다.** 이 섹션이 아래에 남아 있는 **2026-11-09~18 예비 구축 이력보다 최신 상태**입니다.

- **팀원용 지도 다운로드(영구 보관 경로):** [검증 통합 도로망 HTML ZIP](downloads/verified-integrated-road-network-20261120.zip) — *게시 워크플로 성공 후 이 링크에서 다운로드 가능합니다.* ZIP에는 `integrated_road_network_20261120.html`, `viewer_metadata.json`, `README_열기.txt`가 포함됩니다. HTML을 압축 해제해 Chrome/Edge에서 열면 됩니다. **OpenFreeMap 실제 거리·건물·지명 배경은 인터넷이 필요**하며, 도로·시설 데이터는 HTML에 내장되어 있습니다.
- **즉시 다운로드 가능한 검증된 최신 Actions 결과:** [Verified Integrated Road Network HTML 실행 #45017756217](https://github.com/hajunben-gif/Kor_map/actions/runs/45017756217) → 페이지 아래 **Artifacts** → `verified-road-network-html-45017756217-1`. 이 실행은 **성공**했으며 원본 Artifact는 90일 후 만료됩니다. 브라우저에서 실제 배경지도가 표시되는지 사용자 확인까지 마쳤습니다.
- **영구 보관 게시:** 이 변경이 `main`에 병합되면 [게시 워크플로](.github/workflows/publish-verified-road-html.yml)가 **자동 실행**됩니다. 실패한 경우 **Actions → Publish Verified Road Network HTML to Repository → Run workflow**로 재실행하며 기본 ID `45017756217`을 유지합니다. 성공 시 `downloads/verified-integrated-road-network-20261120.zip`과 SHA-256 검증 파일이 `main`에 커밋됩니다. **ZIP이 아직 없다면 다운로드 링크는 작동하지 않습니다.**
- **지도 조작:** 기본 Drive+Service, Drive 기준망, 구호차량 후보망 3개 레이어 전환. 파랑은 일반도로, 주황은 Service, 초록은 위치 확인된 시설 146곳, 빨강은 QA 검토/배정 보정이 발생한 **시설 2곳**(수정초등학교 강당·지곡리회관). 시설 클릭 시 기존 최근접 도로와 양방향 연결 적격 도로까지의 **직선거리** 비교. 왼쪽 아래 버튼으로 온라인 배경지도 표시 전환.
- **데이터 기준:** [Geofabrik South Korea PBF 2026-11-20 스냅샷](docs/INTEGRATED_ROAD_NETWORK_PBF.md), 통합 2km 버퍼 기반 방향성 도로망. Drive **14,390 노드 / 32,085 방향 간선**, Drive+Service **24,457 / 54,893**, 구호차량 후보망 **24,447 / 54,867**. 시설 147개 중 **146개 위치 확인·연결 적격 도로거리 산출**, 대동마을회관 1개는 좌표 미확보. Drive+Service의 평균 연결 적격 도로 직선거리 약 **30.95m**, 최대 **144.13m**, 300m 초과 **0곳**.
- **QA 해석:** 기존 단순 최근접 기준에서 경고 3건(시설 2곳)이 발생했지만 연결 적격 도로로의 **재배정 결과가 산출**되었습니다. 수정초등학교 강당은 고립된 Service 도로 72.67m 대신 일반 기준망과 연결되는 도로 **82.46m**를 선택합니다. 지곡리회관은 Drive 기준망에서 고속도로 본선 대신 **458.20m** 떨어진 적격 도로를 선택하며, Drive+Service에서는 **2.90m** 떨어진 연결된 Service 도로가 유효합니다. 출처: [연결성 QA 성공 실행 #45010076547](https://github.com/hajunben-gif/Kor_map/actions/runs/45010076547). 검증된 기존 결과를 읽는 지도 생성만 수행하므로 전국 PBF 분석은 다시 실행하지 않습니다.
- **한계:** 지도 선형, 노드 연결 및 시설→도로 직선거리 검증이며 실제 주행거리, 교량 하중/폭, 회전금지, 현장 출입구, 차량 통행 허가, 침수 시 도로 통제 여부를 확정한 것이 아닙니다. 침수 시나리오 GIS 중첩·Dijkstra 경로·CFLP/MILP 최적화는 **아직 수행 전**입니다.
- **상세한 생성/검증/사용 설명:** [검증 도로망 HTML 뷰어 문서](docs/VERIFIED_ROAD_NETWORK_HTML.md). 기존 2026-11-09 군별 HTML은 **과거 예비 결과**로 보존합니다.

## 현재 진행 상황

아래 표는 **2026-11-18 당시의 예비 결과 이력**입니다. 현재 상태와 팀원용 HTML 다운로드 방법은 위 **2026-11-22 최신 섹션**을 우선하세요.

| 작업 | 상태와 확인된 결과 |
|---|---|
| 시설 좌표 확보 | 총 147개 중 146개 확보: Kakao Local API 142개 성공 + Google Maps 수동 보완 4개. 청양군 대동마을회관 1개는 미확보 |
| 군별 도로망 예비 구축 | 부여군·청양군 OSMnx `drive` 방향성 도로망 구축 및 저장 완료 |
| 시설별 도로거리 예비 분석 | 좌표 보유 146개에 대해 기존 군별 그래프의 최근접 도로까지 직선거리 계산 완료. 좌표 없는 1개 제외 |
| 군별 행정경계 누락 조사 | 죽림리마을회관의 진입도로·교량 누락 원인 검증 완료 |
| 통합 분석 영역 | 두 군 합집합과 2km·5km 완충 영역 생성·검증 완료 |
| 통합 도로망 구축 | **완료(2026-11-22)**: 11월 20일 PBF 기반 통합 Drive / Drive+Service / 구호차량 후보망 구축·무결성 검사 완료 |
| 통합망 기반 거리 재계산 | **완료(2026-11-22)**: 146개 시설 최근접 거리·양방향 일반도로망 연결 적격 거리 산출, QA 경고 3건 보정 결과 기록 |
| 침수위험·최단경로·최적화 | 아직 미수행 |

Kakao API는 과거 GitHub Actions에서 **실제 호출에 성공**했습니다. 이는 현재 Codex Cloud·사용자 PC·Actions의 API 키 설정 여부나 현재 접속 가능성을 의미하지 않습니다. 이번 문서 갱신에서는 키 설정 확인이나 API 호출을 하지 않았습니다. 초기 인증 대기 기록과 최신 완료 결과의 구분은 [GEOCODING.md](GEOCODING.md)를 참고하세요.

## 시설 좌표와 예비 접근거리

- [최종 시설 CSV](data/processed/shelters_final_147.csv): 147행, 좌표 확보 146개. 식별자는 `시군명 + 시설일련번호`입니다.
- [시설 검증 보고서](data/processed/shelters_validation_report.json): Kakao 142개·수동 4개·미확보 1개와 원본 보존 검증.
- [수동 병합 단계 기록](output/manual_merge/README.md): 실제 Actions 원본과 Google Maps 수동 좌표의 출처. 이 문서의 미계산 설명은 당시 병합 단계의 이력입니다.
- [예비 접근거리 CSV](data/processed/shelters_road_access_147.csv) 및 [분석 보고서](data/processed/shelters_road_access_report.json): EPSG:5179에서 기존 군별 도로 형상까지의 최근접 직선거리와 약연결요소를 기록합니다.
- 예비 지도: [부여군](output/road_access/buyeo_map.html), [청양군](output/road_access/cheongyang_map.html). HTML을 내려받아 브라우저에서 열며, 배경 타일·JS/CSS에는 인터넷이 필요합니다.

주소 대표점과 수동 좌표는 실제 차량 출입구의 현장 확인을 뜻하지 않습니다. 최근접 직선거리도 주행거리나 통행 가능한 진입 경로가 아닙니다. 기존 군별 예비 그래프의 누락이 일부 거리 결과에 영향을 주었으나, 최신 통합망으로 재계산을 완료했습니다. 이 절의 링크는 이전 예비 수치이며 최신 결과는 상단 섹션을 참고하세요.

## 기존 도로망과 검증된 누락 원인

기존 결과는 `data/20261109T045434949410Z/`의 `buyeo/`, `cheongyang/`에 보존돼 있습니다. 각각 `network.graphml`, `network.gpkg`, `boundary.geojson`, `metadata.json`, `map.html`이 있습니다. [RESULTS.md](RESULTS.md)는 **2026-11-09 군별 예비 구축 당시**의 통계·검증 기록입니다.

기존 설정은 OSMnx 2.1.1의 `network_type='drive'`, `simplify=True`, `retain_all=True`, `truncate_by_edge=False`입니다. 죽림리에서는 진입도로와 교량이 단순화 후 군 경계 횡단 간선으로 합쳐지고, 최종 군별 경계 절단에서 간선 전체가 양쪽 그래프에서 제거되는 메커니즘을 검증했습니다. 단순화 자체가 way ID를 버리거나 `residential`을 drive 필터가 제외한 사례가 아닙니다.

| 죽림리 비교 조건 | 시설→도로 직선거리(m) |
|---|---:|
| 기존 청양군 drive | 1,359.551 |
| 기존 부여군 비교 도로 | 259.893 |
| 보존 PBF drive·drive_service·전체 highway | 각각 9.515 |

진입도로는 OSM way `978430790`, 교량은 `978430789`입니다. 자세한 근거·재현 범위는 [검증 결과 정리](output/road_network_audit/jukrim/verified_20261112/README.md), [결론 JSON](output/road_network_audit/jukrim/verified_20261112/cause/conclusion.json), [Cloud 인계](docs/JUKRIM_CLOUD_HANDOFF.md)를 참고하세요. 과거 전체 실행의 완전 재현이나 다른 시설 전체의 누락 원인 검증까지 완료한 것은 아닙니다.

기존 군별 그래프를 사후 병합하는 것만으로 이미 삭제된 도로가 복원되지는 않습니다. OSM 연결·필터 통과도 차량 통행권, 노면 상태, 교량 하중, 폭·높이 제한 또는 구호차량 적합성을 보장하지 않습니다.

## 통합 분석 영역과 다음 도로망 단계

[통합 영역 결과](output/integrated_area/boundaries_20261113/README.md)에는 EPSG:5179·4326 GeoJSON 6개와 출처·해시·검증 기록이 있습니다.

| 영역 | 면적(km²) |
|---|---:|
| 부여군·청양군 합집합 | 1,099.633046 |
| 합집합 + 2km 완충 | 1,481.032321 |
| 합집합 + 5km 완충 | 2,043.666488 |

두 군 ⊆ 통합 경계 ⊆ 2km ⊆ 5km 포함관계와 죽림리 회관·진입도로·교량의 포함을 검증했습니다. **이 영역 생성 설명은 2026-11-13 당시 기록입니다. 2026-11-22 통합 도로 추출·방향성 그래프 구축과 QA를 완료했습니다.**

당시 후속 구축 계획은 2026-11-09의 소스 검토에서 시작했지만, 실제 최신 통합 분석은 **2026-11-20 PBF 스냅샷과 2km 통합 버퍼**를 사용했습니다. 5km 확대는 후속 민감도 검토 대상입니다. 내부 군 경계로 도로를 삭제하지 않고 원본·단순화 그래프를 모두 보존하며, 경계 횡단·교량·방향성·연결성과 죽림리 회귀 검사를 통과한 뒤 146개 시설의 거리를 재계산할 계획입니다.

## 저장소에 있는 자료와 외부 자료

- 포함: 원본 시설 CP949 CSV(`inputs/`), 최종 시설 CSV·검증 보고서, 기존 두 군 도로망·행정경계, 예비 접근거리, 죽림리 지역 조사 증거, 통합 분석 영역.
- **전국 대한민국 원본 PBF와 SQLite 인덱스는 Git에 포함하지 않습니다.** 보존된 지역 JSON 증거와 전국 PBF는 서로 다른 자료입니다. ZIP에 담긴 과거 캐시도 전국 PBF의 대체물이 아닙니다.
- **침수위험지도·침수 시나리오 원자료는 현재 저장소에서 확인되지 않았습니다.** 추적 파일·로컬 자료 파일명과 기존 ZIP 3개의 파일 목록을 확인했으며, 저장소에 포함된 도로망 GeoPackage를 침수자료로 안내하지 않습니다. 향후 공식 자료의 확보·기준 시각·CRS·범례·라이선스 검증이 필요합니다.
- `downloads/`의 ZIP은 과거 단계의 배포·백업 자료입니다. 최신 상태는 위의 개별 파일과 검증 문서를 기준으로 확인하세요. **Code → Download ZIP**도 Git에 없는 외부 자료를 포함하지 않습니다.

## 재현과 실행 구분

Python 3.11 이상을 사용합니다. OSMnx 2.1.1, PyOsmium 4.2.0을 고정하고 있으며 실제 검증 환경은 각 결과의 메타데이터를 기준으로 합니다.

```sh
python -m pip install -r requirements.txt -r requirements-pbf-audit.txt
# 저장된 경계·지역 증거만 사용하는 오프라인 검사
python -m unittest discover -s tests -p test_integrated_area.py -v
python scripts/jukrim_boundary_regression.py
# 통합 영역 재생성: 기존에 존재하지 않는 새 출력 폴더 지정
python scripts/build_integrated_area.py --output work/integrated_area_new
```

패키지 설치에는 네트워크가 필요할 수 있습니다. 위 오프라인 검사는 전국 PBF나 API를 사용하지 않습니다. 합성 입력 테스트는 실제 현장 데이터의 대체 증거가 아닙니다.

`extract.py`는 **기존 군별 예비 구축 방식**이며 통합망 생성 코드가 아닙니다. `python extract.py`는 Nominatim·Overpass를 호출해 새 결과를 생성하므로 단순 결과 열람을 위해 실행할 필요가 없습니다. 기존 방식의 재실행은 검증된 경계 누락 문제를 자동 해결하지 않습니다. PBF 실행 안내는 [JUKRIM_PBF_AUDIT.md](JUKRIM_PBF_AUDIT.md), 지오코딩 재실행 안내는 [ACTIONS_GEOCODING.md](ACTIONS_GEOCODING.md)를 참고하세요. API·다운로드 재실행은 별도 자격·네트워크 접근과 원자료가 필요하며 성공을 보장하지 않습니다.

## 향후 수행할 작업

1. **완료:** 통합 도로망 구축, 경계·교량·방향성·연결성 QA, 시설 146곳 접근거리 재계산 및 팀 공유 HTML 생성.
2. 공식 침수위험자료 확보·검증과 도로 중첩 분석, 침수 시나리오별 통행 제한 정의.
3. 시설·비축 후보지의 실제 진입점 검토와 방향·통행 제한을 반영한 **Dijkstra 최단경로** 및 이동비용 행렬 계산.
4. 후보지·수요·용량·비용·시나리오를 정의한 **CFLP/MILP** 비축거점·배분 최적화와 검증.

현재 저장소는 침수도로 판정, 실제 수송 경로 계산 또는 최적 비축거점 도출을 완료한 결과를 제공하지 않습니다. 시설의 `주거능력`은 최대 수용가능인원이며 실제 이재민 수나 확정 수요량이 아닙니다.

도로 자료 출처: OpenStreetMap contributors / [ODbL 1.0](https://www.openstreetmap.org/copyright). 미터 단위 거리·면적은 EPSG:5179에서 계산하고, 좌표 저장·지도 표시는 EPSG:4326을 사용합니다.
