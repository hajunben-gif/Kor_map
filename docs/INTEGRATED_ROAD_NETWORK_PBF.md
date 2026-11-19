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
