"""Build three profiles from one verified PBF; no route or flood calculation."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

import geopandas as gpd
import networkx as nx
import osmnx as ox
from shapely.geometry import Point, mapping

from scripts import build_integrated_road_network as base
from scripts import integrated_road_graph as core
from scripts.integrated_pbf_source import acquire_profile_pbf
from scripts.jukrim_pbf_audit import file_hash, write_json
from scripts.road_vehicle_policy import vehicle_policy

ROOT = base.ROOT
PROFILES = ("drive_reference", "drive_service", "relief_vehicle_candidate")
PRIOR_RUN = dict(run_id=44920912032, artifact="integrated-road-network-pbf-44920912032-1",
    source_timestamp="2026-11-20T21:21:21Z",
    source_sha256="492ceebb946500fa54dd1b460b4ff923884dd9e673f65d3750e1d60122b85a8e",
    reported_nodes=14390, reported_directed_edges=32085,
    provenance="User-reported prior run; current artifact inventory confirmed, contents not assumed verified")
ARCHIVE_URL = "https://download.geofabrik.de/asia/south-korea-261120.osm.pbf"
OUTLIERS = {"125", "160", "150", "145"}
REPORTED_OUTLIERS = {"125":(551.51,19.74),"160":(425.61,2.90),"150":(360.04,7.81),"145":(345.32,11.60)}
ORDINARY = {"residential", "unclassified", "living_street", "tertiary", "secondary", "primary"}
ACCESS_CONTROLLED = {"motorway", "motorway_link", "trunk", "trunk_link"}


def stable_id(snapshot, parts):
    return hashlib.sha256(core.json_text([snapshot, *parts]).encode()).hexdigest()


def add_identity(graph, roads, snapshot, profile, policies, rejections):
    graph.graph.update(profile=profile, source_sha256=snapshot, vehicle_access_certified=False)
    for u,v,k,attrs in graph.edges(keys=True,data=True):
        way = attrs["osmid"]; current=u; node_ids=[u]; segments=[]
        for ordinal in attrs["raw_segments"]:
            a,b=roads[way]["nodes"][ordinal:ordinal+2]
            following=b if current==a else a
            segments.append(stable_id(snapshot,[way,ordinal,current,following]))
            current=following; node_ids.append(current)
        if current != v:
            raise ValueError("Identity chain endpoint mismatch")
        attrs.update(edge_uid=stable_id(snapshot,["edge",*segments]),
            parent_edge_id=stable_id(snapshot,["edge",*segments]),
            source_segment_ids=";".join(segments), source_node_ids=",".join(map(str,node_ids)),
            drive_included=not rejections["drive_reference"][way],
            drive_service_included=not rejections["drive_service"][way],
            relief_candidate_included=not rejections["relief_vehicle_candidate"][way],
            review_required=True, vehicle_policy_status=policies[way]["status"],
            review_reasons=";".join(policies[way]["review_reasons"]),
            vehicle_passage_confirmed=False)


def anchor_reachability(graph, drive_raw):
    main=max(nx.weakly_connected_components(drive_raw),key=lambda c:(len(c),-min(c)))
    anchors={n for u,v,d in drive_raw.edges(data=True) if d.get("highway") in ORDINARY for n in (u,v) if n in main} & set(graph)
    # Multi-source reachability only; no shortest routes or route costs are computed.
    def closure(reverse):
        seen=set(anchors); pending=list(anchors)
        while pending:
            u=pending.pop()
            for v in (graph.predecessors(u) if reverse else graph.successors(u)):
                if v not in seen:seen.add(v);pending.append(v)
        return seen
    return closure(True),closure(False),anchors


def service_topology(way, roads, raw, connectivity, point):
    to_roads,from_roads,anchors=connectivity
    if way not in roads:
        return {}
    road=roads[way];line=core.metric_line(road["coords"])
    if not any(d["osmid"]==way for _,_,d in raw.edges(data=True)):
        return {"status":"not_in_profile","vehicle_access_confirmed":False}
    # Begin at the nearest segment, respecting each stored direction.
    candidates=[]
    for i,(u,v) in enumerate(zip(road['nodes'],road['nodes'][1:])):
        segment=core.metric_line(road['coords'][i:i+2]);candidates.append((segment.distance(point),i,u,v))
    best=min(v[0] for v in candidates);depart=set();arrive=set();pairs=[]
    for distance,i,u,v in candidates:
        if distance>best+1e-7:continue
        pairs.append([u,v])
        if any(d['osmid']==way and i in d['raw_segments'] for d in raw.get_edge_data(u,v,default={}).values()):
            depart.add(v);arrive.add(u)
        if any(d['osmid']==way and i in d['raw_segments'] for d in raw.get_edge_data(v,u,default={}).values()):
            depart.add(u);arrive.add(v)
    wcc=base.access.wcc_index(raw,'profile')[0];scc=core.strong_index(raw)
    return dict(status="shared_osm_node_topology",near_segment_node_pairs=pairs,
        source_shared_ordinary_node_ids=sorted(set(road['nodes'])&anchors),
        source_shared_other_way_nodes=sorted({n for wid,r in roads.items() if wid!=way for n in set(r['nodes'])&set(road['nodes'])}),
        service_to_ordinary=bool(depart&to_roads),ordinary_to_service=bool(arrive&from_roads),
        weak_components={str(n):wcc[n] for n in road['nodes'] if n in raw},
        strong_components={str(n):scc[n] for n in road['nodes'] if n in raw},
        one_way=core.is_oneway(road['tags']),vehicle_access_confirmed=False,
        limitation="No facility connector, turn-restriction resolution, barrier permission or vehicle certification")


def compare_facilities(original,prior,models,area):
    outputs={};indices={};connections={}
    for name,(raw,simple,_) in models.items():
        results,index=base.compare_facilities(original,prior,raw,simple,area)
        if sum(r['road_access_status']=='matched' for r in results)!=146:
            raise ValueError(f"{name}: not all 146 coordinate holders matched")
        outputs[name]={base.access.identity(r):r for r in results};indices[name]=index
        connections[name]=anchor_reachability(raw,models['drive_reference'][0])
    rows=[]
    for original_row in original:
        identity=base.access.identity(original_row);row=dict(original_row)
        for name in PROFILES:
            result=outputs[name][identity]
            for col in ("road_access_status","road_distance_m","road_edge_osmid","road_edge_highway","road_edge_u","road_edge_v","road_edge_key","road_wcc_id","road_wcc_is_largest","new_u_scc_id","new_v_scc_id","new_edge_return_possible"):
                row[f'{name}_{col}']=result[col]
            if result['road_access_status']=='matched':
                u,v=int(result['road_edge_u']),int(result['road_edge_v'])
                to_roads,from_roads,_=connections[name]
                row[f'{name}_can_reach_ordinary']=v in to_roads
                row[f'{name}_reachable_from_ordinary']=u in from_roads
                row[f'{name}_nearest_access_controlled']=result['road_edge_highway'] in ACCESS_CONTROLLED
                row[f'{name}_actual_vehicle_access']='unverified'
        d=outputs['drive_reference'][identity];s=outputs['drive_service'][identity]
        if d['road_access_status']=='matched':
            delta=float(s['road_distance_m'])-float(d['road_distance_m'])
            if delta>2e-6:
                raise ValueError('Drive-service superset increased geometric nearest distance')
            row['service_minus_drive_m']=f'{delta:.6f}'
            row['distance_reduction_confirms_vehicle_access']=False
            row['original_county_distance_m']=prior[identity]['road_distance_m']
            row['original_six_outlier']=identity==('청양군',identity[1]) and identity[1] in base.REVIEW
            row['newly_topologically_connected_to_ordinary']=(not (row['drive_reference_can_reach_ordinary'] and row['drive_reference_reachable_from_ordinary']) and row['drive_service_can_reach_ordinary'] and row['drive_service_reachable_from_ordinary'])
        rows.append(row)
    return rows,outputs,indices,connections


def export_profiles(payload,models):
    gpkg=payload/'road_network_profiles.gpkg'
    for name,(raw,simple,_) in models.items():
        folder=payload/name;folder.mkdir()
        base.export_graphs(folder,raw,simple)
        shutil.copyfile(folder/'simplified.graphml',payload/f'{name}.graphml')
        # Explicit parent fields in both raw and simplified layers for future flood splits.
        for stage,graph in (('raw',raw),('simplified',simple)):
            nodes,edges=ox.graph_to_gdfs(graph)
            edges['raw_segments']=edges['raw_segments'].map(core.json_text)
            for suffix,frame,keys in (('nodes',nodes,['osmid']),('edges',edges,['u','v','key'])):
                layer=f'{name}_{stage}_{suffix}';frame=frame.reset_index()
                frame.to_file(gpkg,layer=layer,driver='GPKG',engine='pyogrio',index=False)
                restored=gpd.read_file(gpkg,layer=layer)
                if restored.crs.to_epsg()!=4326 or list(restored[keys].itertuples(index=False,name=None))!=list(frame[keys].itertuples(index=False,name=None)) or not restored.geometry.equals(frame.geometry):
                    raise ValueError(f'Profile GeoPackage roundtrip changed {layer}')
                for field in ('edge_uid','parent_edge_id','source_segment_ids','source_node_ids','osm_tags','raw_segments','drive_included','drive_service_included','relief_candidate_included','review_required','vehicle_passage_confirmed','length_5179','oneway','reversed'):
                    if field in frame and restored[field].fillna('').tolist()!=frame[field].fillna('').tolist():
                        raise ValueError(f'Profile trace attribute changed: {layer}/{field}')
        # Check extended trace columns after GraphML reload too.
        loaded=ox.load_graphml(payload/f'{name}.graphml')
        for edge in simple.edges(keys=True):
            for field in ('edge_uid','parent_edge_id','source_segment_ids','source_node_ids'):
                if str(loaded.edges[edge][field])!=str(simple.edges[edge][field]):
                    raise ValueError('GraphML profile trace identity changed')
        (folder/'network.gpkg').unlink() # consolidated, validated GeoPackage is above


def write_rows(path,rows,fields=None):
    if fields is None:fields=list(dict.fromkeys(k for row in rows for k in row))
    base.save_csv(path,fields,rows)


def local_maps(payload,details,roads,original):
    import folium
    for row in details:
        number=row['시설일련번호']
        facility=next(r for r in original if base.access.identity(r)==('청양군',number))
        lat,lon=float(facility['latitude']),float(facility['longitude'])
        point=Point(base.access.TO_METRES.transform(lon,lat))
        view=folium.Map(location=[lat,lon],zoom_start=17,tiles=None)
        folium.Marker([lat,lon],tooltip=facility['시설명']).add_to(view)
        for wid,road in roads.items():
            if core.metric_line(road['coords']).distance(point)>500:continue
            color='#d35400' if road['tags']['highway']=='service' else '#2471a3'
            folium.PolyLine([(y,x) for x,y in road['coords']],color=color,
                tooltip=f"OSM {wid} / {road['tags']['highway']} / vehicle access unverified").add_to(view)
        view.save(payload/f'service_detail_{number}.html')


def render_report(payload,meta,details):
    lines=['# Drive・Service・구호차량 후보 비교','',
        f"PBF: {meta['source']['upstream']}",f"기준 시각: {meta['source']['data_timestamp']}",f"SHA-256: {meta['source']['sha256']}",'',
        '**세 모델은 위 한 개 PBF에서 동시에 생성했습니다. 최근접 직선거리는 주행거리 또는 실제 차량 진입 승인과 다릅니다.**','',
        '| 모델 | 노드 | 방향 간선 | 방향 중복 제거 길이(km) | WCC | SCC |','|---|---:|---:|---:|---:|---:|']
    for r in meta['profile_statistics']:
        lines.append(f"| {r['profile']} | {r['nodes']} | {r['directed_edges']} | {r['undirected_length_m']/1000:.3f} | {r['weak_components']} | {r['strong_components']} |")
    lines+=['','## 시설 거리·연결 비교','', '| 시설 | Drive(m) | Service(m) | 최근접 service way | 포함 | 일반 기준망 최대 WCC 진입/이탈 |','|---|---:|---:|---|---|---|']
    for r in details:
        lines.append(f"| {r['시설명']} | {r['drive_distance_m']} | {r['drive_service_distance_m']} | {r.get('service_way_id','미확인')} | {r.get('drive_service_eligible','')} | {r.get('service_to_ordinary','')} / {r.get('ordinary_to_service','')} |")
    lines+=['',core.json_text(meta['facility_summary']),'',
        f"이전 실행 #44920912032와 동일 PBF 여부: {meta['prior_run_comparison']['same_snapshot']}. 이전 수치는 사용자 제공 이력이며 이번 모델 간 비교와 분리합니다.",
        '근접 service가 없거나 필터에서 제외되면 해당 값을 미확인/제외로 기록합니다. 일반도로 연결은 Drive 최대 WCC의 일반도로 노드를 기준으로 검사합니다. 한쪽 방향 연결, 별도 WCC, motorway/trunk 최근접은 검토 대상입니다.',
        '전 모델의 실제 시설 연결·회전제한·차량 제원·현장 통행은 미확인입니다. 조건부 예외는 자동 허용하지 않습니다.',
        '후보망은 명시적 차량 제한을 제거한 검토 후보이며, 태그가 없다고 통행을 승인하지 않습니다. track은 별도 확장 검토 목록으로만 보존했습니다.',
        '원본과 단순화 그래프, source_segment_ids 대응표, 전체 원본 태그·노드·형상, 제한 relation을 보존했습니다.',
        'source_segment_ids는 스냅샷 SHA-256/way ID/원본 구간 순번/방향 노드쌍으로 결정됩니다. profile 간 비교는 이 ID를 사용합니다. 이후 침수 분할 시 부모 edge_uid를 보존해야 합니다.',
        '최단경로·침수지도 확보·중첩·최적화는 수행하지 않았습니다.','',
        '권고: Drive는 비교 기준, Drive+Service는 원본 확장층, 구호차량 후보는 제한·방향·시설별 진입 검토를 위한 작업층으로 유지합니다. 실제 수송용 확정망은 현장·관리기관 확인 후 선정합니다.',
        'OpenStreetMap contributors / ODbL 1.0. https://www.openstreetmap.org/copyright']
    (payload/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def build(root,source_path,destination,*,checksum=None,source=None,download=False):
    root=Path(root).resolve();destination=Path(destination).resolve()
    if destination.exists():raise FileExistsError(f'Output exists: {destination}')
    if destination.is_relative_to(root):raise ValueError('Use a new output directory outside the checkout')
    hashes=base.input_hashes(root);area=base.load_area(root);counties=base.load_counties(root)
    record=json.loads((root/base.AREA).with_name('validation.json').read_bytes())
    if record['outputs'][base.AREA.name]['sha256']!=hashes[str(base.AREA)]:raise ValueError('Study boundary hash mismatch')
    original,columns,prior=base.load_facilities(root)
    archived=json.loads((root/base.REFERENCE).read_bytes())['source']
    with tempfile.TemporaryDirectory(prefix='road-profiles-') as temp:
        scratch=Path(temp);download_meta=None
        if download:
            source_path,download_meta=acquire_profile_pbf(scratch,
                {'url':ARCHIVE_URL,'sha256':PRIOR_RUN['source_sha256'],'data_timestamp':PRIOR_RUN['source_timestamp']},archived)
            checksum=download_meta['sha256'];source=download_meta['source_url']
        source_info=base.snapshot_metadata(source_path,archived,checksum,source,download_meta)
        roads,extracted=core.read_pbf(source_path,area,scratch)
        rejections={name:core.network_rejections(roads,mode) for name,mode in [('drive_reference','drive'),('drive_service','drive_service')]}
        models={}
        for name in PROFILES[:2]:
            models[name]=core.build_graphs(roads,rejections[name],area,counties,source_path,network_type='drive' if name=='drive_reference' else 'drive_service')
        # Keep tagged nodes even for filtered roads; retain relevant restrictions as evidence.
        import osmium
        selected=set(roads);selected_nodes={n for r in roads.values() for n in r['nodes']}
        node_tags={};restrictions=[]
        for obj in osmium.FileProcessor(source_path,osmium.osm.NODE | osmium.osm.RELATION):
            if obj.is_node() and obj.id in selected_nodes and obj.tags:
                node_tags[obj.id]=dict(obj.tags)
            elif obj.is_relation() and obj.tags.get('type')=='restriction' and any(m.type=='w' and m.ref in selected for m in obj.members):
                restrictions.append(dict(id=obj.id,tags=dict(obj.tags),members=[dict(type=m.type,ref=m.ref,role=m.role) for m in obj.members]))
        service_raw=models['drive_service'][0]
        policies={wid:vehicle_policy(r['tags'],[node_tags.get(n,{}) for n in r['nodes']]) for wid,r in roads.items()}
        rejections['relief_vehicle_candidate']={wid:list(rejections['drive_service'][wid])+([] if policies[wid]['candidate_included'] else [{'policy':'explicit_vehicle_restriction','reasons':policies[wid]['review_reasons']}]) for wid in roads}
        models['relief_vehicle_candidate']=core.build_graphs(roads,rejections['relief_vehicle_candidate'],area,counties,source_path,network_type='drive_service')
        for name,(raw,simple,checks) in models.items():
            core.audit_source_ways(roads,rejections[name],raw,simple,checks)
            for graph in (raw,simple):
                graph.graph.update(data_timestamp=source_info['data_timestamp'],source_url=source_info['upstream'],
                    filter_expression=ox._overpass._get_network_filter('drive' if name=='drive_reference' else 'drive_service'))
            add_identity(raw,roads,source_info['sha256'],name,policies,rejections)
            add_identity(simple,roads,source_info['sha256'],name,policies,rejections)
        segment_sets={name:{d['source_segment_ids'] for _,_,d in raw.edges(data=True)} for name,(raw,_,_) in models.items()}
        if not segment_sets['drive_reference']<=segment_sets['drive_service'] or not segment_sets['relief_vehicle_candidate']<=segment_sets['drive_service']:
            raise ValueError('Profile directed source-segment subset validation failed')
        facility_rows,results,indices,connections=compare_facilities(original,prior,models,area)
        service_ways={wid:r for wid,r in roads.items() if r['tags']['highway']=='service'}
        service_audit=[];details=[]
        for row in original:
            county,number=base.access.identity(row)
            if county!='청양군' or number not in OUTLIERS:continue
            point=Point(base.access.TO_METRES.transform(float(row['longitude']),float(row['latitude'])))
            detail={'시군명':county,'시설일련번호':number,'시설명':row['시설명'],
                'drive_distance_m':results['drive_reference'][county,number]['road_distance_m'],
                'drive_service_distance_m':results['drive_service'][county,number]['road_distance_m'],
                'closest_service_status':'not_found_in_source_extent',
                'prior_run_reported_drive_m':REPORTED_OUTLIERS[number][0],
                'prior_run_reported_service_m':REPORTED_OUTLIERS[number][1],
                'same_snapshot_as_reported_prior_run':source_info['sha256']==PRIOR_RUN['source_sha256']}
            if service_ways:
                wid=min(service_ways,key=lambda w:(core.metric_line(service_ways[w]['coords']).distance(point),w))
                road=roads[wid];topology=service_topology(wid,roads,service_raw,connections['drive_service'],point)
                detail.update(closest_service_status='measured',service_way_id=wid,service_distance_m=point.distance(core.metric_line(road['coords'])),
                    nearest_service_search_covered=point.distance(core.metric_line(road['coords']))<point.distance(area.boundary),
                    drive_service_eligible=not rejections['drive_service'][wid],
                    service_to_ordinary=topology.get('service_to_ordinary','unverified'),ordinary_to_service=topology.get('ordinary_to_service','unverified'),
                    source_nodes=core.json_text(road['nodes']),all_tags=core.json_text(road['tags']),source_geometry=core.json_text(road['coords']),
                    drive_rejections=core.json_text(rejections['drive_reference'][wid]),service_rejections=core.json_text(rejections['drive_service'][wid]),
                    topology=core.json_text(topology),candidate_policy=core.json_text(policies[wid]),
                    field_review='Facility entrance, permission, barriers, road surface, widths, bridge load and relief-vehicle specifications unverified')
            details.append(detail)
        for wid,road in sorted(service_ways.items()):
            service_audit.append(dict(osm_way_id=wid,all_tags=core.json_text(road['tags']),source_node_ids=core.json_text(road['nodes']),source_geometry=core.json_text(road['coords']),
                drive_rejections=core.json_text(rejections['drive_reference'][wid]),drive_service_rejections=core.json_text(rejections['drive_service'][wid]),
                candidate_included=not rejections['relief_vehicle_candidate'][wid],policy=core.json_text(policies[wid])))
        statistics=[dict(profile=name,**checks['statistics']['simplified']) for name,(_,_,checks) in models.items()]
        for r in statistics:
            name=r['profile'];r['service_ways']=sum(not rejections[name][w] for w in service_ways)
            r['service_length_m']=sum(core.metric_line(road['coords']).length for w,road in service_ways.items() if not rejections[name][w])
            r['raw_nodes']=models[name][0].number_of_nodes();r['raw_directed_edges']=models[name][0].number_of_edges()
            r['isolated_nodes']=nx.number_of_isolates(models[name][1])
            r['small_wcc_count']=sum(len(c)<10 for c in nx.weakly_connected_components(models[name][1]))
        facility_summary={name:{'matched':146,'excluded_missing_coordinates':1,
            'thresholds_m':{str(limit):sum(float(r[f'{name}_road_distance_m'])>limit for r in facility_rows if r[f'{name}_road_access_status']=='matched') for limit in (50,100,300)}} for name in PROFILES}
        changes=[float(r['service_minus_drive_m']) for r in facility_rows if 'service_minus_drive_m' in r]
        facility_summary['service_vs_drive']={'decreased':sum(v < -1e-6 for v in changes),'equal':sum(abs(v)<=1e-6 for v in changes),'increased':sum(v>1e-6 for v in changes),
            'newly_topologically_connected':sum(bool(r.get('newly_topologically_connected_to_ordinary')) for r in facility_rows)}
        jukrim={}
        for name,(raw,simple,_) in models.items():
            row=results[name]['청양군','177']
            jukrim[name]=base.review_jukrim(raw,simple,row,json.loads((root/base.REFERENCE).read_bytes()),source_info['same_as_archived_pbf'],'profile_2km')
        meta=dict(status='data_validated',execution_scope='one_pbf_three_profiles',source=source_info,
            created_at_utc=datetime.now(timezone.utc).isoformat(),source_extract=extracted,
            filters={name:ox._overpass._get_network_filter(mode) for name,mode in [('drive_reference','drive'),('drive_service','drive_service')]},
            candidate_policy={'base':'drive_service','rule_version':1,'all_included_edges_require_review':True,
                'vehicle_specifications_supplied':False,'conditional_exceptions_resolved':False,
                'track_included':False,'restrictions_and_barriers_are_not_routing_permissions':True},
            prior_run_comparison=dict(**PRIOR_RUN,same_snapshot=source_info['sha256']==PRIOR_RUN['source_sha256'],historical_numbers_are_current_measurements=False),
            profile_statistics=statistics,facility_summary=facility_summary,graph_validation={n:c for n,(_,_,c) in models.items()},
            input_sha256=hashes,code_sha256={str(p.relative_to(root)):file_hash(p) for p in [root/'scripts/compare_road_profiles.py',root/'scripts/road_vehicle_policy.py',root/'scripts/integrated_road_graph.py']},
            verification={'same_pbf_for_all_profiles':True,'source_directed_segments_preserved':True,'profile_source_segment_subset_checks':True,'vehicle_access_certified':False},
            topology_limits='No turn-restriction/conditional/barrier resolution or facility connector; SCC reachability is only OSM shared-node topology')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.profile-stage-',dir=destination.parent) as staging:
            payload=Path(staging)/'results';payload.mkdir()
            export_profiles(payload,models)
            write_rows(payload/'road_profile_comparison.csv',statistics)
            write_rows(payload/'facilities_access_comparison_147.csv',facility_rows)
            write_rows(payload/'service_road_audit.csv',service_audit,['osm_way_id','all_tags','source_node_ids','source_geometry','drive_rejections','drive_service_rejections','candidate_included','policy'])
            write_rows(payload/'remaining_outliers_4.csv',details)
            trace=[]
            for name,(raw,simple,_) in models.items():
                for u,v,k,d in simple.edges(keys=True,data=True):
                    node_ids=list(map(int,d['source_node_ids'].split(',')))
                    for i,(segment,ordinal) in enumerate(zip(d['source_segment_ids'].split(';'),d['raw_segments'],strict=True)):
                        trace.append(dict(profile=name,source_segment_id=segment,parent_edge_id=d['edge_uid'],
                            osm_way_id=d['osmid'],source_segment_ordinal=ordinal,source_from_node=node_ids[i],
                            source_to_node=node_ids[i+1],u=u,v=v,key=k))
            write_rows(payload/'source_segment_correspondence.csv',trace)
            with (payload/'source_road_audit.jsonl').open('x',encoding='utf-8') as stream:
                for wid,road in sorted(roads.items()):
                    stream.write(core.json_text(dict(osm_way_id=wid,**road,rejections={n:r[wid] for n,r in rejections.items()},vehicle_policy=policies[wid],track_scenario_only=road['tags']['highway']=='track'))+'\n')
            write_json(payload/'restriction_relations.json',restrictions)
            write_json(payload/'source_node_tags.json',node_tags)
            write_json(payload/'jukrim_profiles.json',jukrim)
            local_maps(payload,details,roads,original)
            render_report(payload,meta,details)
            if base.input_hashes(root)!=hashes or file_hash(source_path)!=source_info['sha256']:raise ValueError('Inputs changed during profile comparison')
            meta['verification'].update(inputs_unchanged=True,graphml_gpkg_roundtrips=True,facility_rows_unchanged=True)
            meta['output_sha256']={str(p.relative_to(payload)):file_hash(p) for p in payload.rglob('*') if p.is_file()}
            write_json(payload/'metadata.json',meta)
            if destination.exists():raise FileExistsError(f'Output exists: {destination}')
            payload.rename(destination)
    return meta


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--pbf',type=Path)
    mode.add_argument('--download',action='store_true',help='Try prior 2026-11-20 archive then one official latest PBF')
    parser.add_argument('--sha256');parser.add_argument('--source');parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.download and (args.sha256 or args.source):parser.error('Local provenance arguments require --pbf')
    try:
        meta=build(ROOT,args.pbf,args.output,checksum=args.sha256,source=args.source,download=args.download)
        print(core.json_text({'status':meta['status'],'source':meta['source'],'profile_statistics':meta['profile_statistics'],'facility_summary':meta['facility_summary']}))
    except (OSError,ValueError,RuntimeError) as exc:
        print(f'Road profile comparison failed: {exc}',file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
