# Kakao 주소 지오코딩: 도로망 연결 전 단계

`geocode_shelters.py`는 CP949 시설 CSV에서 부여군30곳·청양군117곳만 선택해 Kakao Local `/v2/local/search/address.json`으로 주소를 조회합니다. 도로망 연결, 진입점 생성 또는 GraphML 변경은 하지 않습니다.

## 현재 실제 실행 상태

업로드한 원본은 1,757행, 대상은 부여군30행·청양군117행입니다. 147개 주소는 서로 다르며, 주소 누락은 없습니다. 현재 `.env`와 `KAKAO_REST_API_KEY`가 없어 **Kakao 호출은 0회**입니다. `output/shelters_geocoded.csv`는 좌표를 채운 결과가 아니라, 147행 모두 `pending_missing_api_key`인 대기 결과입니다. 두 HTML은 군 경계와 안내문만 표시하며 시설 마커가 없습니다. 주소 검색이나 공간 검증을 실제 시설 좌표로 완료했다고 해석하면 안 됩니다.

| 시군 | 대상 | 성공 | 실패 | 수동검토 | 인증 대기 | 성공률 |
|---|---:|---:|---:|---:|---:|---|
| 부여군 | 30 | 0 | 0 | 0 | 30 | 미산출 |
| 청양군 | 117 | 0 | 0 | 0 | 117 | 미산출 |

## 사용자 PC에서 실행

GitHub 저장소를 다운로드하고 압축을 푼 **프로젝트 폴더**에서 PowerShell 또는 터미널을 엽니다. `/workspace/...`는 클라우드 내부 경로이므로 PC에서 사용하지 않습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

`.env`에서 빈 `KAKAO_REST_API_KEY=` 뒤에 본인의 Kakao REST API 키를 입력하고 저장합니다. JavaScript 키나 Admin 키 대신 **REST API 키**를 사용합니다. 실제 키를 코드·README·채팅·GitHub에 쓰지 않습니다. `.env`, `.env.*`는 Git에서 제외하고 값이 비어 있는 `.env.example`만 공유합니다. 환경에 이미 같은 변수가 있으면 기존 환경변수를 우선합니다.

원본 CSV를 `inputs` 폴더에 둔 다음 아래 명령을 실행합니다. 입력 원본 CSV는 저장소에 포함하지 않았습니다.

```powershell
python geocode_shelters.py --csv "inputs/충청남도_재난안전포털 내 이재민 임시주거시설 정보_20251130.csv"
```

기본 `.env`와 경계 경로는 명령을 실행한 폴더 기준입니다. 다른 위치에서는 `--env-file`, `--boundaries`, `--output`, `--cache`로 지정하세요.

```text
--env-file .env
--boundaries data/20261109T045434949410Z
--output output/shelters_geocoded.csv
--cache work/kakao_geocoding_cache
--attempts 3
```

## 출력

- `output/shelters_geocoded.csv`: **원본 9개 컬럼을 순서·값 그대로 유지**하고 `latitude,longitude,geocoding_status,matched_address`와 오류/검증/캐시/시각 컬럼을 추가. UTF-8-SIG라 Excel에서 한글을 읽을 수 있습니다. 입력은 CP949입니다.
- `output/geocoding_summary.json`: 시군별 성공/실패/수동검토/대기 건수, 성공률, API 호출 수, 원본·경계 SHA-256, 좌표계.
- `output/shelters_buyeo.html`, `shelters_cheongyang.html`: 검증된 `success` 시설만 표시. 팝업에 시설명·주소·주거능력을 표시합니다. 배경 타일과 Folium JS/CSS에는 인터넷이 필요합니다.
- `output/shelters_buyeo_review.csv`, `shelters_cheongyang_review.csv`: 실제 실패와 수동 검토 항목. 인증 대기는 실패 목록에 넣지 않습니다.
- `output/geocoding_candidates.jsonl`: 실제 API 후보 원자료. 확실하지 않은 좌표는 이 파일에서 검토하며 결과 CSV의 위도·경도에는 넣지 않습니다.
- `output/geocoding_api_audit.jsonl`: 요청별 시각·HTTP 상태·재시도와 원인. Authorization 헤더와 키는 기록하지 않습니다.

시설일련번호가 군 사이에 중복되어 **시군명+시설일련번호**를 식별자로 사용합니다. 원본 번호를 임의 재부여하지 않습니다. 주거능력은 **최대 수용가능인원**으로 실제 이재민 수가 아닙니다.

## 좌표 채택 기준

Kakao `x`를 경도(`longitude`), `y`를 위도(`latitude`)로 기록합니다. WGS84(EPSG:4326), GeoJSON Point 순서는 `(longitude, latitude)`, Folium은 `[latitude, longitude]`입니다.

`analyze_type=exact`로 조회하고 후보의 충청남도·시군명, 도로명, 건물 본번·부번이 입력 주소와 일치하는지 확인합니다. 일치 후보가 한 좌표로 수렴하면 기존 OSM 행정경계 폴리곤 안인지 검사합니다. 후보가 여러 좌표이거나 주소 일치가 불확실하거나 경계 밖이면 `manual_review`로 분류하고 위도·경도를 비워 둡니다. 좌표를 중심점 등으로 대체하지 않습니다.

기존 경계는 부여군 OSM relation7578882, 청양군 relation7619553입니다. 공식 측량 경계와 일치한다고 보장하지 않으므로 경계 밖 후보는 오답으로 단정하지 않고 검토로 남깁니다. 입력 시설 자료 기준일은 파일명의 2025-11-30, 경계는 기존 OSM 추출 기준입니다. API 호출 시각은 별도로 기록합니다. 주소 대표점은 실제 시설 출입구나 차량 진입점이 아닙니다.

## 오류·재시도·캐시

`failed_no_result`는 검색 결과 없음, `manual_review`는 모호한/불일치/경계 밖 후보, `failed_api`는 HTTP·연결 오류입니다. 실패 코드·원인은 CSV 및 감사 로그에 남깁니다. HTTP429/500/502/503/504와 연결 오류는 기본 최대3회 시도하고 지수 대기·Retry-After를 적용합니다. Retry-After가 60초를 초과하면 조기에 다시 호출하지 않고 다음 실행으로 보류합니다. 401/403 또는 재시도 소진429 이후에는 남은 요청을 `pending_api_unavailable`로 보류해 인증/할당량 오류를 반복 호출하지 않습니다.

정규화한 같은 주소는 메모리 캐시로 중복 호출하지 않습니다. HTTP200의 정상 응답만 디스크에 캐시하며 다음 실행에 재사용합니다. 오류 응답은 디스크에 캐시하지 않습니다. 캐시는 키를 포함하지 않습니다. 원자료가 갱신됐을 때 새로 조회하려면 캐시를 다른 위치로 이동하고 실행하세요.

대기 항목이 있으면 **성공률은 null(미산출)**입니다. 완료 후 성공률은 `success/전체 대상 시설 수`이며 수동검토는 분모에 포함되고 성공에 포함되지 않습니다. 현재 확보한 좌표 비율은 `verified_coordinate_coverage`로 따로 기록합니다. 실행 종료코드2는 대기 항목 존재, 0은 대기 없는 처리 종료입니다. 종료코드0이어도 일부 실제 실패·수동검토가 있을 수 있으므로 요약을 확인하세요.

## 테스트

```powershell
python -m pip install pytest
python -m pytest -q tests/test_geocoding.py
```

모의 Kakao 응답으로 12개 테스트를 통과했습니다. 경도/위도 순서, 실제 OSM 경계에 대한 공간 검사, 주소·시군 불일치, 복수 후보와 무검색, 429 재시도, 캐시, 인증 차단 및 키 비노출을 검사했습니다. **모의 응답 테스트 통과는 실제 시설의 지오코딩 성공을 뜻하지 않습니다.**
