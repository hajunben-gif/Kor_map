# 100년 빈도 침수지도 SHP 검증 — GitHub Actions 실행 가이드

## 상태

- GitHub Actions: \`.github/workflows/validate-flood-shp-100y.yml\`
- 파서: \`scripts/audit_flood_shp_100y.py\`
- 안전한 ZIP 입력 점검: \`scripts/prepare_flood_source_zip.py\`
- 회귀 테스트: \`tests/test_flood_shp_100y.py\`
- **가상 데이터 회귀 테스트 성공**: [Actions 실행 #45174037182](https://github.com/hajunben-gif/Kor_map/actions/runs/45174037182)
- **사용자 원본 SHP에 대한 실행은 아직 미완료**. 테스트 성공 ≠ 실제 침수심 속성 검증 완료.

## 실제 자료를 1개의 ZIP으로 묶기

사용자의 로컬 PC에서 다음 20개 파일을 **이름을 바꾸지 않고** 한 ZIP에 넣는다. 하위 폴더가 있어도 처리 가능하다.

| 대상 | 파일 접두사 | 필수 확장자 |
|---|---|---|
| 부여 국가 | \`RFM_SGG_NTN_44760_100\` | .shp, .shx, .dbf, .prj, .cpg |
| 부여 지방 | \`RFM_SGG_RGN_44760_100\` | .shp, .shx, .dbf, .prj, .cpg |
| 청양 국가 | \`RFM_SGG_NTN_44790_100\` | .shp, .shx, .dbf, .prj, .cpg |
| 청양 지방 | \`RFM_SGG_RGN_44790_100\` | .shp, .shx, .dbf, .prj, .cpg |

**중요:** 이 프로젝트는 공개 GitHub 저장소다. 원본 ZIP이나 SHP를 공개 저장소, 공개 Release, 공개 URL에 업로드하기 전에 **원자료의 재배포 허가 여부를 확인**한다. 입력 URL에 서명 토큰이나 개인 비밀번호를 넣지 않는다. GitHub Actions 실행 인수 및 로그는 공개될 수 있다.

## 옵션 A. 로컬 분석 (가장 간단하고 비공개)

\`\`\`bash
python -m unittest discover -s tests -p test_flood_shp_100y.py -v
python -m scripts.prepare_flood_source_zip \
  --zip ./flood_100y_sources.zip --output-dir ./flood_100y_raw
python -m scripts.audit_flood_shp_100y \
  --input-dir ./flood_100y_raw --output-dir ./flood_100y_audit
\`\`\`

파일을 재배포할 필요 없이 사용자 PC에서 \`flood_100y_audit/flood_shp_audit_100y.json\` 및 \`.md\`를 확인할 수 있다. 도형 유효성 검사까지 수행하려면 \`pip install geopandas\`.

## 옵션 B. GitHub Actions (외부 HTTPS URL 필요)

원본 데이터 제공 약관상 재배포가 허용되고, 신뢰할 수 있는 HTTPS 다운로드 URL이 있을 경우:

1. GitHub에서 저장소 \`Actions\` → **Validate 100-Year Flood SHP Data** → **Run workflow**를 연다.
2. \`source_zip_url\`에 20개 파일이 든 ZIP의 **직접 다운로드 가능한 HTTPS URL**을 입력한다. 로그인·대화형 다운로드가 필요한 URL은 동작하지 않는다.
3. \`source_zip_sha256\`에 ZIP의 SHA-256 체크섬 64자리를 입력한다. Windows PowerShell: \`(Get-FileHash .\flood_100y_sources.zip -Algorithm SHA256).Hash\`, macOS/Linux: \`shasum -a 256 flood_100y_sources.zip\`.
4. 실행 후 **Audit real 100-year flood SHP** 작업이 성공했는지 확인한다.
5. \`flood-shp-100y-qa-<run_id>-<attempt>\` Actions 아티팩트에서 JSON 및 Markdown 검증 결과를 내려받는다.

다운로드 URL을 입력하지 않으면 **가상 데이터 셀프 테스트만 실행**된다. 입력 URL은 HTTPS이며 정확한 SHA-256이 필수다. 원본 SHP 자체는 Actions 아티팩트로 게시하지 않는다.

## 검증에서 볼 항목

- \`attributes.fields\`: 필드명, 자료형, 실제 고유값/빈도
- \`shape\`: Shape type, 레코드 수, 원본 좌표 범위
- \`crs\`: EPSG:5186 투영 파라미터 검증
- \`geometry_qa\`: GeoPandas 유효 도형, 빈 도형, 원본 피처 면적 합(중복 제거 전)
- \`source_files\`: 각 원본의 SHA-256

**침수심 코드의 실제 물리적 수심 의미는 공식 범례 및 데이터 정의서와 대조한 뒤 확정해야 한다.**
**차단 시나리오 S0~S2 및 도로망 중첩은 아직 실행하지 않는다.**
