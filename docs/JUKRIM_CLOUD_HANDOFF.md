# 죽림리마을회관 Codex Cloud 인계

작업 상태: 실제 PBF 지역 분석과 누락 원인 조사는 완료됐으며, 검증 코드·결과를 Git에 보존한다. **전체 도로망 재구축은 아직 승인되지 않았다.**

먼저 [검증 결과 정리](../output/road_network_audit/jukrim/verified_20261112/README.md)와 `manifest.json`을 읽는다. 기존 결과 5개, 원인 조사 핵심 증거, 과거 캐시의 작은 지역 부분집합이 저장돼 있다. 원인 보고서 원문은 일부 한국어가 이미 `?`로 손실돼 있으므로 별도 정리 문서를 읽는다. 원본 증거는 변경하지 않았다.

## 환경 및 오프라인 재현

저장소 루트에서 Python 3.11 이상을 사용한다. 기존 의존성에 OSMnx 2.1.1, PyOsmium 4.2.0이 고정돼 있다. 재현 중에는 외부 API나 전국 PBF가 필요 없다. 패키지가 없는 Cloud 환경에서는 먼저 의존성을 준비한다.

```sh
python -m pip install -r requirements.txt -r requirements-pbf-audit.txt
python -m unittest discover -s tests -p test_jukrim_pbf_audit.py -v
python scripts/jukrim_boundary_regression.py
```

마지막 명령은 저장된 두 지역 입력과 기존 경계만 사용한다. 실제 설치된 OSMnx 함수의 다운로드 반환값에 작은 입력을 주입하고 네트워크를 차단한다. 6개 설정 결과 및 입력 해시 불변을 확인하며 기존 GraphML·CSV나 결과를 쓰지 않는다. 새 JSON 출력이 필요하면 존재하지 않는 경로를 `--output`으로 지정한다.

PBF 분석 스크립트의 `prepare-local`, `extract`, `analyze`는 별도 기능이다. 위 회귀 검사 때문에 이를 다시 실행할 필요는 없다. `.github/workflows/jukrim-pbf-audit.yml`은 수동 workflow_dispatch이며 다운로드·실제 추출을 수행하므로 이번 인계에서 실행하지 않는다.

## 보존된 구현

- `scripts/jukrim_pbf_audit.py`: 로컬 원본 출처·해시 검증, 원본 way 보존 추출, 불완전 객체의 outside/relevant/unknown 분류, 실패 인덱스 재사용 차단, 지역 ID만 메모리에 두는 복사 개선.
- `tests/test_jukrim_pbf_audit.py`: 합성 PBF 검사 12개. 합성 자료는 실제 조사 증거로 쓰지 않는다.
- `scripts/jukrim_boundary_regression.py`: 절대 Windows 경로·ZIP·원본 PBF가 필요 없는 역사적 지역 회귀 재현.

원본 PBF와 SQLite는 Git에 넣지 않는다. 향후 실제 추출을 지시받더라도 실패·중단 인덱스를 검증 없이 재사용하지 않는다. 기존 GraphML·대피시설 CSV·과거 분석 결과를 덮어쓰지 않는다.

## 다음 단계의 판단 기준

검증한 원인은 단순화된 경계 횡단 간선의 후속 엄격한 절단이다. `truncate_by_edge=True` 또는 동등한 외부 연결 보존 정책, 충분한 분석 범위가 필요하다. 아직 생산 `extract.py` 설정을 수정하거나 전체 군 그래프를 재구축하지 않았다. 실제 차량 통행성도 미확인이다. 추가 작업은 사용자의 다음 지시에 맞춰 범위를 정한다.
