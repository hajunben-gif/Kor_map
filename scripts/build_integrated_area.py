"""Offline administrative union and buffers only; no road/PBF processing."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess

import numpy as np
import pyproj
from pyproj import CRS, Transformer
import shapely
from shapely.geometry import Point, shape, mapping
from shapely.ops import unary_union
from shapely.validation import explain_validity

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = Path('data/20261109T045434949410Z')
EVIDENCE = Path('output/road_network_audit/jukrim/verified_20261112/pbf_results/evidence.json')
METRIC = CRS.from_epsg(5179)
WGS = CRS.from_epsg(4326)
BUFFER_QUAD_SEGS = 64
ROUNDTRIP_TOLERANCE_M = .01


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')


def valid_polygon(geometry, label):
    if geometry.geom_type not in ('Polygon', 'MultiPolygon') or geometry.is_empty:
        raise ValueError(f'{label}: nonempty Polygon/MultiPolygon required')
    if geometry.has_z or not all(math.isfinite(v) for v in geometry.bounds):
        raise ValueError(f'{label}: finite 2D coordinates required')
    if not geometry.is_valid:
        raise ValueError(f'{label}: {explain_validity(geometry)}')
    return geometry


def reproject(geometry, source, destination):
    transformer = Transformer.from_crs(source, destination, always_xy=True)
    return shapely.transform(geometry, lambda coords: np.column_stack(
        transformer.transform(coords[:, 0], coords[:, 1])))


def load_boundary(path, region, expected_sha=None):
    raw = path.read_bytes()
    if expected_sha is not None and sha(raw) != expected_sha:
        raise ValueError(f'{path}: source differs from committed boundary')
    data = json.loads(raw)
    if data.get('type') != 'FeatureCollection' or not data.get('features'):
        raise ValueError(f'{path}: nonempty FeatureCollection required')
    declaration = data.get('crs')
    if declaration is None:
        crs_name = 'OGC:CRS84'  # RFC 7946 GeoJSON longitude/latitude default.
    else:
        if declaration.get('type') != 'name':
            raise ValueError(f'{path}: unsupported CRS declaration')
        crs_name = declaration['properties']['name']
    crs = CRS.from_user_input(crs_name)
    geometries = []
    for feature in data['features']:
        if feature.get('properties', {}).get('region') != region:
            raise ValueError(f'{path}: administrative region identity mismatch')
        geometries.append(valid_polygon(shape(feature['geometry']), str(path)))
    native = valid_polygon(unary_union(geometries), str(path))
    geographic = valid_polygon(reproject(native, crs, WGS), 'geographic boundary')
    x1,y1,x2,y2 = geographic.bounds
    if not (125 < x1 < x2 < 128 and 35 < y1 < y2 < 38):
        raise ValueError(f'{path}: outside expected Chungnam geographic extent')
    metric = valid_polygon(reproject(native, crs, METRIC), 'metric boundary')
    if metric.area <= 0:
        raise ValueError('Nonpositive administrative area')
    return metric, dict(path=str(path), sha256=sha(raw), bytes=len(raw), region=region,
        declared_crs=crs_name, crs_wkt=crs.to_wkt(),
        crs_assumed_from_rfc7946=declaration is None, area_m2=metric.area,
        committed_hash_verified=expected_sha is not None)


def make_areas(counties):
    union = valid_polygon(unary_union(list(counties.values())), 'union')
    areas = {'union':union, 'buffer_2000m':union.buffer(2000, quad_segs=BUFFER_QUAD_SEGS),
             'buffer_5000m':union.buffer(5000, quad_segs=BUFFER_QUAD_SEGS)}
    for name,g in areas.items():
        valid_polygon(g,name)
    if not all(union.covers(g) for g in counties.values()):
        raise ValueError('Union does not cover both input counties')
    if not areas['buffer_2000m'].covers(union) or not areas['buffer_5000m'].covers(areas['buffer_2000m']):
        raise ValueError('Buffer containment failed')
    if not union.area < areas['buffer_2000m'].area < areas['buffer_5000m'].area:
        raise ValueError('Area monotonicity failed')
    return areas


def geojson(geometry, name, epsg):
    data = {'type':'FeatureCollection','features':[{'type':'Feature',
        'properties':{'scope':name,'epsg':epsg},'geometry':mapping(geometry)}]}
    if epsg != 4326:
        # Explicit projected GeoJSON for GIS tools; WGS84 counterparts are RFC7946.
        data['crs']={'type':'name','properties':{'name':f'EPSG:{epsg}'}}
    return data


def build(root, destination):
    root=Path(root).resolve(); destination=Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f'Output already exists; refusing overwrite: {destination}')
    for protected in ('data','inputs'):
        if destination.is_relative_to(root/protected):
            raise ValueError('Output must not be in an input directory')
    paths=subprocess.check_output(['git','ls-files','-z'],cwd=root).split(b'\0')
    protected={p.decode():sha((root/p.decode()).read_bytes()) for p in paths if p and (root/p.decode()).is_file()}
    counties={}; sources={}
    for slug,region in [('buyeo','부여군'),('cheongyang','청양군')]:
        relative=BOUNDARIES/slug/'boundary.geojson'
        committed=subprocess.check_output(['git','show',f'HEAD:{relative.as_posix()}'],cwd=root)
        counties[slug],sources[slug]=load_boundary(root/relative,region,sha(committed))
    areas=make_areas(counties)
    payloads={}; validation={}
    for name,g in areas.items():
        geographic=valid_polygon(reproject(g,METRIC,WGS),name+' WGS84')
        # Validate actual serialized coordinates, not only the in-memory transform.
        wgs_bytes=encoded(geojson(geographic,name,4326))
        restored=reproject(shape(json.loads(wgs_bytes)['features'][0]['geometry']),WGS,METRIC)
        error=g.hausdorff_distance(restored)
        area_error=abs(g.area-restored.area)/g.area
        if not restored.is_valid or error>ROUNDTRIP_TOLERANCE_M or area_error>1e-8:
            raise ValueError(f'{name}: CRS roundtrip validation failed')
        payloads[f'{name}_epsg4326.geojson']=wgs_bytes
        payloads[f'{name}_epsg5179.geojson']=encoded(geojson(g,name,5179))
        validation[name]={'valid':True,'geometry_type':g.geom_type,'area_m2':g.area,
            'area_km2':g.area/1e6,'bounds_epsg5179':list(g.bounds),
            'roundtrip_hausdorff_m':error,'roundtrip_relative_area_error':area_error}
    evidence_path=root/EVIDENCE; evidence_bytes=evidence_path.read_bytes(); evidence=json.loads(evidence_bytes)
    committed_evidence=subprocess.check_output(['git','show',f'HEAD:{EVIDENCE.as_posix()}'],cwd=root)
    if sha(evidence_bytes)!=sha(committed_evidence):
        raise ValueError('Control evidence differs from committed reference')
    checks=[]
    objects=[('facility','죽림리마을회관',Point(126.836798762428,36.3667897253441))]
    for way_id in (978430790,978430789):
        road=next(r for r in evidence['roads'] if r['way_id']==way_id)
        geometry=shape(road['geometry'])
        if geometry.geom_type!='LineString' or len(road['nodes'])!=len(geometry.coords):
            raise ValueError('Control way node/geometry mismatch')
        objects.append(('way',str(way_id),geometry))
        for node_id,coordinate in zip(road['nodes'],geometry.coords):
            objects.append(('node',str(node_id),Point(coordinate)))
    for kind,identifier,geometry in objects:
        metric=reproject(geometry,WGS,METRIC)
        checks.append({'kind':kind,'id':identifier,'geometry_wgs84':mapping(geometry),
            'membership':{name:{'covered':g.covers(metric),'intersects':g.intersects(metric)}
                          for name,g in {**counties,**areas}.items()}})
    if not all(c['membership']['union']['covered'] for c in checks):
        raise ValueError('Known Jukrim facility/road/bridge not fully covered by union')
    if any(sha((root/p).read_bytes())!=h for p,h in protected.items()):
        raise ValueError('Existing tracked input changed during processing')
    report={'status':'validated','created_at_utc':datetime.now(timezone.utc).isoformat(),
        'code_base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'script_sha256':sha(Path(__file__).read_bytes()),
        'versions':{'shapely':shapely.__version__,'pyproj':pyproj.__version__,'proj':pyproj.proj_version_str},
        'policy':{'metric_crs':'EPSG:5179','coordinate_order':'always_xy',
            'buffer_distances_m':[2000,5000],'buffer_quad_segs':BUFFER_QUAD_SEGS,
            'buffer_method':'planar EPSG:5179, round joins; circular arcs polygonally approximated',
            'roundtrip_tolerance_m':ROUNDTRIP_TOLERANCE_M,'geometry_repair_applied':False},
        'sources':sources,'control_evidence':{'path':str(EVIDENCE),'sha256':sha(evidence_bytes)},
        'validation':validation,'containment':{'union_covers_both_counties':True,
            'buffer_2000_covers_union':True,'buffer_5000_covers_buffer_2000':True},
        'county_overlap_m2':counties['buyeo'].intersection(counties['cheongyang']).area,
        'control_checks':checks,'outputs':{name:{'sha256':sha(value),'bytes':len(value)} for name,value in payloads.items()},
        'preservation':{'existing_tracked_files_checked':len(protected),'unchanged':True},
        'scope':'Boundary polygons only; no road extraction, graph construction, PBF processing or facility access recalculation.'}
    destination.mkdir(parents=True,exist_ok=False)
    for name,value in payloads.items():
        with (destination/name).open('xb') as f:f.write(value)
    with (destination/'validation.json').open('xb') as f:f.write(encoded(report))
    lines=['# 부여군·청양군 통합 분석 영역 검증','',
        '행정경계와 완충 폴리곤만 생성했습니다. 도로 추출·PBF 처리·그래프 변환은 수행하지 않았습니다.','',
        '| 범위 | 면적(km²) | 형상 | WGS84 왕복 최대 형상 오차(m) |',
        '|---|---:|---|---:|']
    for name,v in validation.items():
        lines.append(f"| {name} | {v['area_km2']:.6f} | {v['geometry_type']} | {v['roundtrip_hausdorff_m']:.10f} |")
    lines += ['', '두 군 ⊆ 통합 경계 ⊆ 2km 완충 ⊆ 5km 완충을 확인했습니다.',
        '회관 좌표·진입도로 978430790 전체·교량 978430789 전체 및 각 참조 노드가 세 통합 범위에 포함됩니다.',
        '완충은 EPSG:5179 평면 거리이며 원호는 사분원당 64개 선분으로 근사합니다. 실제 도로망의 연결성이나 완충 범위의 충분성을 검증한 결과는 아닙니다.',
        '원본 CRS·SHA-256·Git 기준 대조·면적·좌표 변환 오차·개별 지점 포함관계·결과 파일 해시는 validation.json에 기록했습니다.',
        'EPSG:4326 GeoJSON은 RFC7946 경도/위도 순서이며, EPSG:5179 GeoJSON은 명시적 CRS를 넣은 GIS용 파생본입니다.','',
        '재실행 시 존재하지 않는 새 출력 폴더를 지정해야 합니다. 기존 결과는 덮어쓰지 않습니다.']
    with (destination/'README.md').open('x',encoding='utf-8') as f:f.write('\n'.join(lines)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New output directory only')
    args=parser.parse_args()
    report=build(ROOT,args.output)
    print(json.dumps({'status':report['status'],'validation':report['validation'],
        'containment':report['containment'],'output':str(args.output)},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
