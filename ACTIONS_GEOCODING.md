# GitHub Actions 주소 변환

워크플로 이름: **Kakao Shelter Geocoding** (`.github/workflows/kakao-shelters.yml`).

1. 첨부된 원본 CP949 CSV가 `inputs/충청남도_재난안전포털 내 이재민 임시주거시설 정보_20251130.csv`에 포함되어 있습니다. 전체 1,757행 중 부여군30·청양군117행이며 대상 상세주소 누락은 없습니다. 기본 경로로 실행할 수 있습니다.
2. 저장소 Actions Secret `KAKAO_REST_API_KEY`에 REST API 키를 설정합니다.
3. Actions → Kakao Shelter Geocoding → Run workflow에서 입력 CSV의 저장소 상대 경로를 지정합니다.
4. 실행의 Artifacts에서 `kakao-shelters-<run_id>-<attempt>`를 다운로드합니다.

입력 존재 여부와 Git 추적 여부, CP949, 필수 열, 부여군30·청양군117행을 검사한 뒤에만 API를 호출합니다. 기존 `geocode_shelters.py`의 주소 일치·행정경계 검증을 그대로 사용합니다. 경도는 longitude(x), 위도는 latitude(y), 좌표계는 EPSG:4326입니다. 결과 CSV는 Excel용 UTF-8-SIG입니다.

성공·실패·수동검토·보류는 결과 CSV와 요약 JSON, 요청별 상태는 감사 JSONL에 기록됩니다. 검증된 실제 좌표만 두 군별 HTML 지도에 표시됩니다. 지도 배경과 JS/CSS는 인터넷이 필요합니다. 모든 시설이 검증되지 않으면 Actions 실행은 실패로 표시되며, 확보된 실제 결과와 진단은 artifact에 보존됩니다.

`execution_status.json`에 입력 누락, 프록시 연결, TLS/네트워크, 인증(401), 접근 거부(403), 할당량(429), 잘못된 응답 등을 구분합니다. HTTP403만으로 API 권한 거부와 프록시 거부를 확정할 수 없으므로 `FORBIDDEN_API_OR_PROXY`로 보고합니다. 검색 결과 없음은 CSV의 `failed_no_result`입니다. 연결 사전 검사가 실패하면 결과 CSV와 지도를 생성하지 않습니다. 가짜 좌표나 대체 중심점을 만들지 않습니다.

키는 Secret에서 프로세스 환경변수로만 전달하며 출력·파일 저장·artifact 업로드하지 않습니다. 캐시는 업로드하지 않습니다. 입력 CSV와 기존 도로망 파일은 수정하지 않으며, 매 실행 결과는 새 `work/actions-results/`에만 생성합니다. 기존 `output/`의 대기 결과는 업로드하지 않습니다.
