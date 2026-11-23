# 100년 빈도 침수심 SEG_CODE ↔ 공식 색상 직접 대조

## 목적

정의가 아직 확인되지 않은 `N330~N334`의 실제 침수심 구간을 **추측 없이** 확인하는 시각 검증 도구입니다.

- 원본 SHP의 코드를 5개의 **임시 식별 색상**으로 지도에 표시합니다. 이 임시 색은 실제 침수심을 뜻하지 않습니다.
- 사용자가 다운로드했던 공식 PNG를 같은 화면 오른쪽에 표시합니다. **PNG에는 좌표정보가 없으며 기하학적 자동 정합은 수행하지 않습니다.**
- 코드별 대표점 20개를 CSV로 기록합니다.
- 좌표계 EPSG:5186 → 지도로 표시할 때 EPSG:4326로 변환합니다.
- 사용자 원본 파일은 수정하지 않고, 수심 코드 순서는 자동 확정하지 않습니다.

## 내 PC에서 실행

기존 다운로드한 Kor_map 폴더의 `scripts` 안에 `verify_flood_seg_code.py`만 추가하면 됩니다.

PowerShell을 Kor_map 폴더에서 열고 아래 명령을 한 줄로 실행합니다.

```powershell
py -m pip install folium geopandas
py -m scripts.verify_flood_seg_code --input-dir "$env:USERPROFILE\Desktop\flood_100y_raw" --source-zip "$env:USERPROFILE\Desktop\flood_100y_sources.zip" --output-dir "$env:USERPROFILE\Desktop\flood_seg_code_review"
```

기존 바탕화면에 `flood_seg_code_review` 폴더가 있다면 덮어쓰기 대신 다른 이름으로 출력해야 합니다.

## 생성 파일

- `code_comparison.html`: **먼저 열 파일**, 공식 이미지와 SHP 지도 좌우 비교
- `code_overlay_map.html`: SHP 코드별 공간 지도. 우상단 레이어 메뉴에서 지역/국가·지방하천 변경. 팝업 툴팁에는 SEG_CODE 표시
- `representative_points.csv`: 코드 20건의 확인용 대표 좌표(위도·경도)
- `manifest.json`: 코드 매핑 미확정 및 이미지 수집 현황

공식 이미지가 보이지 않으면 `manifest.json`의 `image_keys_found`를 확인합니다. ZIP의 PNG 이름은 `RFM_SGG_NTN_44760_100.png` 또는 `RFM_SGG_NTN_44760_100(1).png` 등이어야 합니다. ZIP에 이미지가 없는 경우 공식 PNG를 원본 ZIP에 추가하거나 시각 대조는 별도로 수행할 수 있습니다.

**공식 PNG와 SHP 지도에서 같은 지역을 충분히 확대해서 5개 코드의 위치와 색상을 대조**합니다. 중앙부가 가장 깊다는 가정 대신 실제 색상과 지리적 위치가 일치하는지 여러 지역에서 검증합니다. 공식 침수심 범례: 0.5m 이하 / 0.5–1m / 1–2m / 2–5m / 5m 이상.

인터넷이 연결되어 있으면 OpenFreeMap 지도가 배경으로 나타납니다. 인터넷이 없거나 CDN이 차단되면 코드 폴리곤만 표시됩니다.

## 중요: 결과의 엄격한 구분

이 도구의 산출물은 코드→수심 대응이 **확정되었다는 증거가 아닙니다.** 임시 코드색으로 공식 이미지의 패턴을 대조하는 보조도구입니다. 실제 코드를 공식 수심에 대응시킨 표를 만들고 상이한 지역/하천에서 교차검증하기 전에는 S1/S2 도로 차단에 사용하지 않습니다.

기존 `integrate_flood_100y.py`의 좌표재투영 오류 수정을 방해하지 않습니다. 다른 경로의 독립 단계이며 100년 빈도 통합 범위 생성 자체는 수심 해석 없이 가능하지만, 통행 제한 시나리오에는 코드 검증이 필수입니다.
