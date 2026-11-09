"""Kakao-only shelter address geocoding. Does not modify/connect road graphs."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import time
import folium
import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
import requests
from shapely.geometry import Point

ENDPOINT = 'https://dapi.kakao.com/v2/local/search/address.json'
TARGETS = {'부여군': ('buyeo', 30), '청양군': ('cheongyang', 117)}
REQUIRED = ['시설일련번호','시군명','시설명','상세주소','주거능력']
NEW_COLUMNS = ['latitude','longitude','geocoding_status','matched_address',
               'geocoding_error_code','geocoding_error_message','geocoding_attempts',
               'geocoding_cache_hit','boundary_validation','geocoding_source',
               'geocoding_checked_at_utc','geocoding_attempt_history']
CAPACITY_NOTE = '주거능력은 최대 수용가능인원이며 실제 이재민 수가 아닙니다.'


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def normal_address(address):
    return re.sub(r'\s+', ' ', str(address)).strip()


def road_signature(address):
    # Facility input is road-name addresses; never reduce to a road/county centroid.
    match = re.search(r'([^\s]+(?:대로|로|길))\s+(\d+)(?:-(\d+))?(?![\d-])', address)
    return (match[1], int(match[2]), int(match[3] or 0)) if match else None


def retry_delay(header, attempt):
    if header:
        try:
            return max(0., float(header))
        except ValueError:
            try:
                return max(0., (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return float(2 ** (attempt - 1))


class KakaoClient:
    def __init__(self, key, cache_dir, session=None, sleep=time.sleep, attempts=3, interval=.25):
        self.key = key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.attempts = attempts
        self.interval = interval
        self.memory = {}
        self.circuit = None
        self.http_calls = 0

    def safe_message(self, value):
        text = str(value)
        if self.key:
            text = text.replace(self.key, '[REDACTED]')
        return text[:300]

    def search(self, address):
        query = normal_address(address)
        cache_key = hashlib.sha256(json.dumps({'query':query, 'analyze_type':'exact', 'size':30},
                                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if cache_key in self.memory:
            return {**self.memory[cache_key], 'cache_hit':True}
        path = self.cache_dir / f'{cache_key}.json'
        if path.exists():
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached.get('query') == query and isinstance(cached.get('payload', {}).get('documents'), list):
                result = {'payload':cached['payload'], 'cache_hit':True, 'attempts':0,
                          'history':[], 'error_code':'', 'error_message':''}
                self.memory[cache_key] = result
                return result
        if not self.key:
            return {'pending':'pending_missing_api_key', 'cache_hit':False, 'attempts':0,
                    'history':[], 'error_code':'MISSING_API_KEY', 'error_message':'환경변수 KAKAO_REST_API_KEY 미설정'}
        if self.circuit:
            return {**self.circuit, 'pending':'pending_api_unavailable', 'cache_hit':False,
                    'attempts':0, 'history':[]}
        history = []
        payload = None
        error_code, error_message = '', ''
        for attempt in range(1, self.attempts + 1):
            self.sleep(self.interval)
            response = None
            try:
                self.http_calls += 1
                response = self.session.get(ENDPOINT, params={'query':query, 'analyze_type':'exact', 'size':30},
                                           headers={'Authorization':f'KakaoAK {self.key}'}, timeout=(10,30))
                status = response.status_code
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if status == 200 and isinstance(body, dict) and isinstance(body.get('documents'), list):
                    payload = body
                    history.append({'attempt':attempt, 'http_status':200, 'at_utc':utcnow()})
                    error_code, error_message = '', ''
                    save_json(path, {'query':query, 'retrieved_at_utc':utcnow(), 'payload':payload})
                    break
                error_code = f'HTTP_{status}' if status != 200 else 'INVALID_RESPONSE'
                detail = body.get('message', body.get('errorType', '')) if isinstance(body, dict) else 'JSON 형식 오류'
                error_message = self.safe_message(detail or f'Kakao HTTP {status}')
                history.append({'attempt':attempt, 'http_status':status, 'error_code':error_code,
                                'reason':error_message, 'at_utc':utcnow()})
                if status in (401,403):
                    self.circuit = {'error_code':error_code, 'error_message':error_message}
                    break
                retryable = status in (429,500,502,503,504) or error_code == 'INVALID_RESPONSE'
                if not retryable:
                    break
            except requests.RequestException as exc:
                error_code = type(exc).__name__
                error_message = 'Kakao 연결/요청 시간 오류'
                history.append({'attempt':attempt, 'error_code':error_code, 'reason':error_message, 'at_utc':utcnow()})
            if attempt < self.attempts:
                delay = retry_delay(response.headers.get('Retry-After') if response is not None else None, attempt)
                if delay > 60:
                    self.circuit = {'error_code':error_code, 'error_message':f'Retry-After {delay:g}초: 다음 실행으로 보류'}
                    break
                self.sleep(delay)
        if payload is None and error_code == 'HTTP_429' and self.circuit is None:
            self.circuit = {'error_code':error_code, 'error_message':error_message}
        result = {'payload':payload, 'cache_hit':False, 'attempts':len(history), 'history':history,
                  'error_code':error_code, 'error_message':error_message}
        self.memory[cache_key] = result
        return result


def classify(payload, address, county, boundary):
    docs = payload.get('documents', [])
    base = {'latitude':None, 'longitude':None, 'matched_address':'', 'boundary_validation':'not_checked'}
    if not docs:
        return {**base, 'geocoding_status':'failed_no_result', 'geocoding_error_code':'NO_RESULT',
                'geocoding_error_message':'주소검색 결과 없음'}
    if payload.get('meta', {}).get('total_count', len(docs)) > len(docs):
        return {**base, 'geocoding_status':'manual_review', 'geocoding_error_code':'TOO_MANY_RESULTS',
                'geocoding_error_message':'반환 범위를 초과한 후보가 있어 수동 확인 필요'}
    signature = road_signature(normal_address(address))
    matches = []
    for doc in docs:
        road = doc.get('road_address') or {}
        parcel = doc.get('address') or {}
        county_value = road.get('region_2depth_name') or parcel.get('region_2depth_name')
        province = road.get('region_1depth_name') or parcel.get('region_1depth_name')
        if county_value != county or province not in ('충청남도','충남') or signature is None:
            continue
        try:
            candidate_sig = (road['road_name'], int(road['main_building_no']), int(road.get('sub_building_no') or 0))
            lon, lat = float(doc['x']), float(doc['y'])
        except (KeyError, ValueError, TypeError):
            continue
        if candidate_sig == signature and 124 < lon < 132 and 33 < lat < 39:
            matches.append((lon,lat,doc))
    if not matches:
        return {**base, 'geocoding_status':'manual_review', 'geocoding_error_code':'ADDRESS_MISMATCH',
                'geocoding_error_message':'시군·도로명·건물번호의 정확한 일치를 확인하지 못함'}
    positions = {(lon,lat) for lon,lat,_ in matches}
    if len(positions) != 1:
        return {**base, 'geocoding_status':'manual_review', 'geocoding_error_code':'AMBIGUOUS_COORDINATES',
                'geocoding_error_message':'일치하는 주소에 복수 좌표가 반환됨'}
    lon,lat,doc = matches[0]
    matched_address = (doc.get('road_address') or {}).get('address_name') or doc.get('address_name','')
    if not boundary.covers(Point(lon,lat)):
        return {**base, 'geocoding_status':'manual_review', 'matched_address':matched_address,
                'boundary_validation':'outside_county', 'geocoding_error_code':'OUTSIDE_BOUNDARY',
                'geocoding_error_message':'Kakao 시군명이 일치하나 OSM 행정경계 밖에 위치함'}
    return {**base, 'latitude':lat, 'longitude':lon, 'matched_address':matched_address,
            'boundary_validation':'inside_county', 'geocoding_status':'success',
            'geocoding_error_code':'', 'geocoding_error_message':''}


def load_input(path):
    original = pd.read_csv(path, encoding='cp949', dtype=str, keep_default_na=False)
    if not set(REQUIRED).issubset(original.columns):
        raise ValueError('필수 입력 컬럼 누락: '+str(sorted(set(REQUIRED)-set(original.columns))))
    if set(NEW_COLUMNS).intersection(original.columns):
        raise ValueError('좌표/결과 컬럼이 이미 존재합니다. 좌표 없는 원본 CSV를 사용하세요.')
    target = original[original['시군명'].str.strip().isin(TARGETS)].copy()
    for county,(_,expected) in TARGETS.items():
        if (target['시군명'].str.strip() == county).sum() != expected:
            raise ValueError(f'{county} 예상 시설 수 {expected}건 불일치')
    keys = pd.DataFrame({'county':target['시군명'].str.strip(),'id':target['시설일련번호'].str.strip()})
    if keys.duplicated().any() or (keys['id']=='').any():
        raise ValueError('동일 시군 내 시설일련번호 중복 또는 누락')
    return target


def render_maps(frame, boundaries, output_folder):
    for county,(slug,_) in TARGETS.items():
        polygon = boundaries[county]
        x1,y1,x2,y2 = polygon.bounds
        selected = frame[frame['시군명'].str.strip() == county]
        located = selected[selected['geocoding_status'] == 'success']
        map_ = folium.Map(location=[(y1+y2)/2,(x1+x2)/2], zoom_start=11, tiles='OpenStreetMap')
        folium.GeoJson(gpd.GeoSeries([polygon],crs=4326).__geo_interface__,name='OSM 행정경계',
                       style_function=lambda _:{'fillOpacity':0,'color':'#555','weight':2}).add_to(map_)
        for row in located.to_dict('records'):
            popup = '<br>'.join(html.escape(str(t)) for t in [row['시설명'],row['상세주소'],
                 f"최대 수용가능인원(주거능력): {row['주거능력']}", CAPACITY_NOTE,
                 'Kakao 주소 좌표: 정확한 차량 진입점은 미확인'])
            folium.Marker([row['latitude'],row['longitude']], tooltip=html.escape(row['시설명']),
                          popup=folium.Popup(popup,max_width=400)).add_to(map_)
        note = f'{county}: 좌표 확인 {len(located)}/{len(selected)}곳<br>{CAPACITY_NOTE}<br>WGS84(EPSG:4326), 주소검색·행정경계 검증을 통과한 시설만 표시'
        if not len(located):
            note += '<br>현재 표시할 검증된 시설 좌표가 없습니다. 인증 대기와 실패는 CSV에서 구분합니다.'
        map_.get_root().html.add_child(folium.Element('<div style="position:fixed;bottom:15px;left:15px;z-index:9999;background:white;padding:12px">'+note+'</div>'))
        map_.fit_bounds([[y1,x1],[y2,x2]])
        folium.LayerControl().add_to(map_)
        map_.save(str(output_folder/f'shelters_{slug}.html'))


def run(args):
    load_dotenv(dotenv_path=args.env_file, override=False)
    key = os.environ.get('KAKAO_REST_API_KEY','').strip()
    if key in ('your_api_key_here','YOUR_KAKAO_REST_API_KEY'):
        key = ''
    frame = load_input(args.csv)
    original_columns = list(frame.columns)
    boundary_paths = {county:args.boundaries/slug/'boundary.geojson' for county,(slug,_) in TARGETS.items()}
    boundaries, hashes = {}, {}
    for county,path in boundary_paths.items():
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        gdf = gpd.read_file(path)
        if gdf.crs is None:
            raise ValueError(f'{county} 경계 CRS 누락')
        polygon = gdf.to_crs(4326).geometry.union_all()
        if polygon.is_empty or not polygon.is_valid or polygon.geom_type not in ('Polygon','MultiPolygon'):
            raise ValueError(f'{county} 경계 폴리곤 오류')
        boundaries[county] = polygon
    args.output.parent.mkdir(parents=True, exist_ok=True)
    client = KakaoClient(key, args.cache, attempts=args.attempts)
    results, audit, candidates = [], [], []
    for row in frame.to_dict('records'):
        county = row['시군명'].strip()
        address = normal_address(row['상세주소'])
        result = {'latitude':None,'longitude':None,'matched_address':'','boundary_validation':'not_checked',
                  'geocoding_source':'Kakao Local address API', 'geocoding_checked_at_utc':utcnow()}
        if not address:
            response = {'cache_hit':False,'attempts':0,'history':[],'error_code':'EMPTY_ADDRESS','error_message':'상세주소 누락'}
            result.update(geocoding_status='failed_empty_address')
        else:
            query = address if county in address else f'충청남도 {county} {address}'
            response = client.search(query)
            if response.get('pending'):
                result['geocoding_status'] = response['pending']
            elif response.get('payload') is None:
                result['geocoding_status'] = 'failed_api'
            else:
                result.update(classify(response['payload'], address, county, boundaries[county]))
                candidates.append({'시군명':county,'시설일련번호':row['시설일련번호'], 'query':query,
                                   'documents':response['payload']['documents']})
        result.setdefault('geocoding_error_code',response.get('error_code',''))
        result.setdefault('geocoding_error_message',response.get('error_message',''))
        result.update(geocoding_attempts=response['attempts'], geocoding_cache_hit=response['cache_hit'],
                      geocoding_attempt_history=json.dumps(response['history'],ensure_ascii=False))
        results.append({**row,**result})
        if response['history']:
            audit.append({'시군명':county,'시설일련번호':row['시설일련번호'], 'address':address,
                          'attempts':response['history']})
    output = pd.DataFrame(results).reindex(columns=original_columns+NEW_COLUMNS)
    output.to_csv(args.output,index=False,encoding='utf-8-sig')
    summary = {'run_at_utc':utcnow(), 'input_csv_sha256':hashlib.sha256(args.csv.read_bytes()).hexdigest(),
               'input_encoding':'CP949','output_encoding':'UTF-8-SIG','output_csv':str(args.output),
               'crs':'EPSG:4326','http_calls':client.http_calls,'input_rows':len(frame),
               'unique_addresses':frame['상세주소'].map(normal_address).nunique(),
               'capacity_note':CAPACITY_NOTE,'boundary_sha256':hashes,
               'boundary_source':'Previously extracted OpenStreetMap administrative relations; not a cadastral boundary',
               'coordinate_note':'Address representative coordinates, not confirmed facility entrances', 'regions':{}}
    for county,(slug,_) in TARGETS.items():
        subset = output[output['시군명'].str.strip()==county]
        states = subset['geocoding_status']
        success = int((states=='success').sum())
        failed = int(states.str.startswith('failed_').sum())
        manual = int((states=='manual_review').sum())
        pending = int(states.str.startswith('pending_').sum())
        summary['regions'][county] = {'total':len(subset),'success':success,'failed':failed,
          'manual_review':manual,'pending':pending,'success_rate':success/len(subset) if not pending else None,
          'verified_coordinate_coverage':success/len(subset),'status_counts':states.value_counts().to_dict()}
        subset[states.str.startswith('failed_') | (states=='manual_review')].to_csv(args.output.parent/f'shelters_{slug}_review.csv',index=False,encoding='utf-8-sig')
        print(county, json.dumps(summary['regions'][county],ensure_ascii=False),flush=True)
    save_json(args.output.parent/'geocoding_summary.json',summary)
    for filename,records in [('geocoding_api_audit.jsonl',audit),('geocoding_candidates.jsonl',candidates)]:
        (args.output.parent/filename).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
    render_maps(output,boundaries,args.output.parent)
    print('API 요청:',client.http_calls,'회; 결과:',args.output)
    return 2 if any(v['pending'] for v in summary['regions'].values()) else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--boundaries',type=Path,default=Path('data/20261109T045434949410Z'))
    parser.add_argument('--output',type=Path,default=Path('output/shelters_geocoded.csv'))
    parser.add_argument('--cache',type=Path,default=Path('work/kakao_geocoding_cache'))
    parser.add_argument('--attempts',type=int,default=3)
    args=parser.parse_args()
    if args.attempts<1:
        parser.error('--attempts must be positive')
    return run(args)


if __name__=='__main__':
    raise SystemExit(main())
