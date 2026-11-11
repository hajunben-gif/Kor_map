# 죽림리마을회관 OSM 조회 실패 기록

- 요청: 2026-11-11 23:54:31 KST (14:54:31 UTC), 반경 2km, 죽림리마을회관 1곳.
- 실행 환경에서 `overpass-api.de` 허용 및 현재 정책 적용(enforced)을 확인했습니다.
- 기존 코드의 `python output/road_network_audit/jukrim/audit.py --fetch`를 별도 작업 트리에서 정확히 한 번 실행했습니다. 종료 코드 2. 재시도하지 않았습니다.
- 결과: `HTTP 406 Not Acceptable`, 대상 `https://overpass-api.de/api/interpreter`. 오류 발생 원인은 응답 코드만으로 확정할 수 없습니다. 도메인 차단 또는 기존 403 오류로 단정하지 않았습니다.
- 최신 OSM 원자료를 확보하지 못했습니다. 최신 drive·drive_service·범용 도로 비교, 연결성·교량·통행 제한 검증은 미완료입니다. 기존 수치를 최신 결과로 재사용하지 않았습니다.
- 실제 요청 쿼리·시각·오류는 `fetch_failure.json`, 실행·정책 확인 요약은 `attempt_metadata.json`에 있습니다.
- 원본 GraphML·시설 CSV·기존 결과는 보존했습니다. 기존 스크립트가 실패 시 작성하는 보고서에는 403 오류가 고정되어 있어, 그 생성 보고서는 새 결과에 포함하지 않았습니다. 기존 코드는 수정하지 않았습니다.
