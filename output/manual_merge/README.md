# 최종 시설 좌표 병합 결과

**147행 중 실제 좌표 146곳, 위치 미확인 1곳입니다.**

- Kakao API 성공 좌표 142개와 해당 행의 기존 모든 열은 원본과 동일합니다.
- 사용자 제공 Google Maps (manual) 좌표 4개만 병합했습니다. 네 시설의 이름은 각각 유일하며 모두 청양군 소속, 기존 OSM 청양군 행정경계 내부입니다.
- 수동 좌표는 별도 현장 검증을 거치지 않았습니다. 주소 대표점은 차량 진입점 확인을 의미하지 않습니다.
- 대동마을회관은 좌표 공란, location_unverified, road_accessibility_eligible=false입니다. 제외 사유를 CSV에 기록했습니다.
- 통합 지도에 146개 마커와 두 군의 기존 OSMnx 자동차 도로망 및 행정경계를 포함했습니다. 파랑은 Kakao, 주황은 Google Maps 수동 좌표입니다.
- 도로 접근성 점수·최단 경로·도로 스냅은 계산하지 않았습니다. 접근성 대상 플래그는 좌표 유무에 대한 사전 필터입니다.
- 원본 도로망·경계 파일은 변경하지 않았으며 SHA-256 전후 비교로 확인했습니다.
- 지도 HTML에 시설·도로 데이터가 포함되어 있습니다. 배경 타일 및 Leaflet 라이브러리는 인터넷이 필요합니다.

## 파일

`shelters_final_147.csv`: 최종 147행 (UTF-8-SIG). `latitude`=위도(y), `longitude`=경도(x), WGS84.

`shelters_roads_integrated.html`: 시설 146곳 및 기존 도로망 통합 지도.

`kakao_actions_original.csv`: 실제 Actions artifact의 원본 CSV (변경 없음).

`manual_coordinate_audit.csv`: 네 수동 좌표의 시설 식별·주소·경계·출처·현장 미검증 기록.

`accessibility_excluded.csv`: 위치 미확인으로 접근성 계산에서 제외하는 1곳.

`merge_validation.json`: 수량, 출처, 기존 성공 행 불변 검증 및 파일 해시.

## 출처

[실제 GitHub Actions 실행 42587230808, attempt 2](https://github.com/hajunben-gif/Kor_map/actions/runs/42587230808/attempts/2), artifact ID 13659149237.

원본 142행은 geocoding_status=success를 유지합니다. 네 수동 좌표는 success_manual, 출처는 Google Maps (manual)입니다. 기존 API 오류 기록은 original_geocoding_* 열에 보존했습니다. manual_validation_note와 field_verification_status에 현장 미검증을 기록했습니다. geocoding_checked_at_utc는 원래 API 조회 시각을 유지하고 수동 병합 시각은 coordinate_recorded_at_utc로 구분했습니다.
