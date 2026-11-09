"""Mock Kakao responses for logic tests; these are NOT facility geocoding results."""
from pathlib import Path
import sys
import json
import pytest
import geopandas as gpd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import geocode_shelters as geo

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def boundary():
    return gpd.read_file(ROOT/'data/20261109T045434949410Z/buyeo/boundary.geojson').to_crs(4326).geometry.union_all()


def doc(point, county='부여군', main='729', sub=''):
    return {'x':str(point.x),'y':str(point.y),'address_name':'충청남도 부여군 은산면 충의로 729',
            'road_address':{'region_1depth_name':'충청남도','region_2depth_name':county,
                            'road_name':'충의로','main_building_no':main,'sub_building_no':sub,
                            'address_name':'충청남도 부여군 은산면 충의로 729'}}


def test_x_longitude_y_latitude_and_inside_boundary(boundary):
    p=boundary.representative_point()
    result=geo.classify({'documents':[doc(p)]},'충청남도 부여군 은산면 충의로 729','부여군',boundary)
    assert result['geocoding_status']=='success'
    assert result['longitude']==p.x and result['latitude']==p.y
    assert result['boundary_validation']=='inside_county'


@pytest.mark.parametrize('county,main',[('청양군','729'),('부여군','72'),('부여군','7290')])
def test_wrong_county_or_number_is_manual_review(boundary,county,main):
    result=geo.classify({'documents':[doc(boundary.representative_point(),county,main)]},'충의로 729','부여군',boundary)
    assert result['geocoding_status']=='manual_review'
    assert result['longitude'] is None


def test_multi_candidates_choose_only_exact_address(boundary):
    point=boundary.representative_point()
    result=geo.classify({'documents':[doc(point,main='72'),doc(point)]},'충의로 729','부여군',boundary)
    assert result['geocoding_status']=='success'


def test_multiple_exact_addresses_with_different_coordinates_need_review(boundary):
    p=boundary.representative_point()
    from shapely.geometry import Point
    other=Point(p.x+.0001,p.y)
    result=geo.classify({'documents':[doc(p),doc(other)]},'충의로 729','부여군',boundary)
    assert result['geocoding_error_code']=='AMBIGUOUS_COORDINATES'
    assert result['latitude'] is None


def test_no_result_does_not_create_coordinates(boundary):
    result=geo.classify({'documents':[]},'충의로 729','부여군',boundary)
    assert result['geocoding_status']=='failed_no_result' and result['latitude'] is None


def test_outside_county_does_not_enter_map(boundary):
    from shapely.geometry import Point
    result=geo.classify({'documents':[doc(Point(127.5,37.0))]},'충의로 729','부여군',boundary)
    assert result['geocoding_error_code']=='OUTSIDE_BOUNDARY'
    assert result['geocoding_status']=='manual_review' and result['longitude'] is None


class Response:
    def __init__(self,status,payload,headers=None):
        self.status_code=status
        self.payload=payload
        self.headers=headers or {}
    def json(self):return self.payload

class Session:
    def __init__(self,responses):self.responses=iter(responses);self.calls=0
    def get(self,*args,**kwargs):
        self.calls+=1
        return next(self.responses)


def test_429_retried_retry_after_recorded_and_disk_cache_reused(tmp_path):
    sleeps=[]
    session=Session([Response(429,{'message':'rate limited'},{'Retry-After':'3'}),Response(200,{'documents':[]})])
    client=geo.KakaoClient('unit-test-not-real',tmp_path,session,sleep=sleeps.append,interval=0)
    result=client.search('충의로 729')
    assert result['attempts']==2 and result['history'][0]['error_code']=='HTTP_429'
    assert 3 in sleeps and session.calls==2
    assert client.search('  충의로   729 ')['cache_hit']
    another=geo.KakaoClient('unit-test-not-real',tmp_path,Session([]),sleep=sleeps.append)
    assert another.search('충의로 729')['cache_hit']
    assert another.http_calls==0


def test_auth_failure_circuit_breaker_and_key_redaction(tmp_path):
    key='unit-test-not-real-secret'
    session=Session([Response(401,{'message':'unauthorized '+key})])
    client=geo.KakaoClient(key,tmp_path,session,sleep=lambda _:None)
    first=client.search('첫 주소')
    second=client.search('둘째 주소')
    assert first['error_code']=='HTTP_401'
    assert second['pending']=='pending_api_unavailable' and session.calls==1
    assert key not in json.dumps([first,second])
    assert not list(tmp_path.glob('*.json'))


def test_no_key_no_api_and_never_counted_as_failure(tmp_path):
    client=geo.KakaoClient('',tmp_path,Session([]),sleep=lambda _:None)
    assert client.search('주소')['pending']=='pending_missing_api_key'
    assert client.http_calls==0


def test_subbuilding_number():
    assert geo.road_signature('충청남도 청양군 장평면 지천로 1241-6')==('지천로',1241,6)
    assert geo.road_signature('중앙로40길 22-3')==('중앙로40길',22,3)
