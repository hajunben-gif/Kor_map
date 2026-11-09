"""Validate tracked CP949 input, probe Kakao, then run the existing geocoder.

Never log request headers, exception text, or API response bodies.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from geocode_shelters import load_input, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', required=True, type=Path)
    parser.add_argument('--output-dir', type=Path, default=Path('work/actions-results'))
    args = parser.parse_args()
    out = args.output_dir.resolve()
    # Fresh output only; never mix old pending results into this run's artifact.
    out.mkdir(parents=True, exist_ok=False)

    def report(category, **details):
        payload = {'status': category, **details}
        (out / 'execution_status.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(payload, ensure_ascii=False))

    try:
        source = args.csv.resolve()
        relative = source.relative_to(ROOT)
        tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', '--', str(relative)],
                                 cwd=ROOT, capture_output=True).returncode == 0
        if not source.is_file() or not tracked:
            report('INPUT_MISSING', message='입력 CSV가 저장소에 추적되는 파일로 존재해야 합니다.')
            return 1
        if source.is_relative_to(out) or relative.parts[0] in ('output', 'work'):
            report('INVALID_INPUT', message='생성 결과 CSV를 원본 입력으로 사용할 수 없습니다.')
            return 1
        # Strict decoding plus existing schema/count checks (30 + 117).
        with source.open(encoding='cp949', newline='') as f:
            list(csv.DictReader(f))
        frame = load_input(source)
    except (ValueError, OSError, UnicodeError):
        report('INVALID_INPUT', message='CP949 인코딩, 필수 열, 부여군30/청양군117 행 수를 확인하세요.')
        return 1

    key = os.environ.get('KAKAO_REST_API_KEY', '').strip()
    if not key:
        report('MISSING_SECRET')
        return 1
    first = frame.iloc[0]
    address = str(first['상세주소']).strip()
    if first['시군명'].strip() not in address:
        address = f"충청남도 {first['시군명'].strip()} {address}"
    try:
        response = requests.get('https://dapi.kakao.com/v2/local/search/address.json',
                                params={'query': address, 'analyze_type': 'exact'},
                                headers={'Authorization': 'KakaoAK ' + key},
                                timeout=(10, 30), allow_redirects=False)
    except requests.exceptions.ProxyError:
        report('PROXY_CONNECTION_FAILED')
        return 1
    except requests.exceptions.SSLError:
        report('TLS_ERROR')
        return 1
    except requests.RequestException:
        report('NETWORK_ERROR')
        return 1
    status = response.status_code
    if status != 200:
        category = ('AUTHENTICATION_FAILED' if status == 401 else
                    'FORBIDDEN_API_OR_PROXY' if status == 403 else
                    'RATE_LIMITED' if status == 429 else 'HTTP_ERROR')
        report(category, http_status=status)
        return 1
    try:
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get('documents'), list):
            raise ValueError
    except ValueError:
        report('INVALID_API_RESPONSE', http_status=status)
        return 1
    print('Kakao 사전 연결 확인: HTTP 200')
    # Cache/output paths are isolated; input and road network are read-only.
    options = argparse.Namespace(csv=source, env_file=ROOT/'work/nonexistent-actions.env',
        boundaries=ROOT/'data/20261109T045434949410Z',
        output=out/'shelters_geocoded.csv', cache=out/'cache', attempts=3)
    try:
        code = run(options)
        summary = json.loads((out/'geocoding_summary.json').read_text(encoding='utf-8'))
        errors = sum(v['failed'] + v['manual_review'] + v['pending']
                     for v in summary['regions'].values())
        report('COMPLETED_WITH_ERRORS' if errors or code else 'SUCCESS',
               http_status=status, regions=summary['regions'])
        return 1 if errors or code else 0
    except Exception:
        report('PROCESSING_ERROR', message='변환 처리 오류. 비밀 보호를 위해 예외 원문은 기록하지 않습니다.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
