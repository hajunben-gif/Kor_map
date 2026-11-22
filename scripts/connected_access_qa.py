"""Read-only QA of saved road-profile artifacts; no PBF/API/route computation."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

import networkx as nx
import osmnx as ox
from shapely.geometry import Point
from shapely.strtree import STRtree

from scripts import build_integrated_road_network as base
from scripts import compare_road_profiles as profiles
from scripts import integrated_road_graph as core
from scripts.jukrim_pbf_audit import file_hash, write_json

ROOT=base.ROOT
TARGET_WAY=1228606370
TARGET_KEY=('청양군','18')
# User-supplied observations, not recomputed values or stable component identities.
REPORTED_CASE={'facility_key':TARGET_KEY,'way_id':TARGET_WAY,'drive_distance_m':82.462631,
    'drive_service_distance_m':72.665399,'saved_wcc_id':'integrated_wcc_082',
    'can_reach_ordinary':False,'reachable_from_ordinary':False,'source':'user_reported_20261122'}


def reference_core(drive):
    """Choose one strongly connected ordinary-road anchor set in Drive's main WCC."""
    if not drive.number_of_edges():raise ValueError('Empty Drive reference graph')
    main=max(nx.weakly_connected_components(drive),key=lambda c:(len(c),-min(c)))
    ordinary={n for u,v,d in drive.edges(data=True) if d.get('highway') in profiles.ORDINARY for n in (u,v)}
    candidates=[]
    for group in nx.strongly_connected_components(drive.subgraph(main)):
        internal_ordinary=any(u in group and v in group and d.get('highway') in profiles.ORDINARY
                              for u,v,d in drive.subgraph(group).edges(data=True))
        if internal_ordinary:candidates.append((group,group&ordinary))
    if not candidates:raise ValueError('No strongly connected ordinary-road reference core')
    group,anchors=max(candidates,key=lambda pair:(len(pair[1]),len(pair[0]),-min(pair[0])))
    return anchors,dict(main_wcc_nodes=len(main),reference_scc_nodes=len(group),
        ordinary_anchor_nodes=len(anchors),anchor_osm_ids=sorted(anchors),
        selection='Drive largest WCC; SCC with most ordinary-road nodes, then size, then minimum OSM ID')


def active_reference_anchors(graph, anchors):
    present=set(anchors)&set(graph)
    groups=[g for g in nx.strongly_connected_components(graph) if g&present
            and any(d.get('highway') in profiles.ORDINARY for _,_,d in graph.subgraph(g).edges(data=True))]
    if not groups:return set(),{'status':'reference_core_absent','active_anchor_nodes':0}
    group=max(groups,key=lambda g:(len(g&present),len(g),-min(g)))
    return group&present,{'status':'selected','active_anchor_nodes':len(group&present),
        'active_scc_nodes':len(group),'minimum_active_node':min(group&present)}


def edge_eligibility(graph,anchors):
    """u->v is round-trip connected iff core reaches u and v reaches the same core."""
    present,_=active_reference_anchors(graph,anchors)
    def closure(reverse):
        reached=set(present);stack=list(present)
        while stack:
            n=stack.pop()
            for other in (graph.predecessors(n) if reverse else graph.successors(n)):
                if other not in reached:reached.add(other);stack.append(other)
        return reached
    to_core,from_core=closure(True),closure(False)
    wcc,_=base.access.wcc_index(graph,'qa')
    scc=core.strong_index(graph)
    rows=[];eligible=[]
    for u,v,key,d in graph.edges(keys=True,data=True):
        ingress=u in from_core;egress=v in to_core
        controlled=d.get('highway') in profiles.ACCESS_CONTROLLED
        ok=ingress and egress and not controlled
        reason=[]
        if not ingress:reason.append('no_core_to_edge_entry')
        if not egress:reason.append('no_edge_exit_to_core')
        if controlled:reason.append('access_controlled_highway_not_facility_target')
        if ok:eligible.append((u,v,key))
        rows.append(dict(u=u,v=v,key=key,osm_way_id=d['osmid'],edge_uid=d.get('edge_uid',''),
            source_segment_ids=d.get('source_segment_ids',''),wcc_id=wcc[u],
            u_scc=scc[u][0],v_scc=scc[v][0],core_to_entry=ingress,exit_to_core=egress,
            round_trip_topology=ingress and egress,eligible_facility_target=ok,
            rejection_reasons=';'.join(reason),actual_vehicle_access='unverified'))
    return eligible,rows,to_core,from_core


def nearest_matches(index,point):
    hit=index.nearest(point)
    candidates=index.tree.query(point,predicate='dwithin',distance=hit['distance']+base.access.TIE_TOLERANCE_M)
    tied=sorted(index.keys[int(i)] for i in candidates
                if point.distance(index.lines[int(i)])<=hit['distance']+base.access.TIE_TOLERANCE_M)
    return hit,tied


def evaluate_facilities(original,graphs,area):
    anchors,core_info=reference_core(graphs['drive_reference'])
    indices={};qualified={};states={};eligibility_rows=[]
    core_info['per_profile']={}
    for profile,graph in graphs.items():
        _,core_info['per_profile'][profile]=active_reference_anchors(graph,anchors)
        eligible,rows,to_core,from_core=edge_eligibility(graph,anchors)
        indices[profile]=base.spatial_index(graph)
        qualified[profile]=base.spatial_index(graph.edge_subgraph(eligible).copy()) if eligible else None
        states[profile]={ (r['u'],r['v'],r['key']):r for r in rows }
        eligibility_rows.extend(dict(profile=profile,**row) for row in rows)
    output=[];alerts=[]
    for original_row in original:
        row=dict(original_row);key=base.access.identity(row)
        if not row['latitude'] and not row['longitude']:
            row['qa_status']='excluded_missing_coordinates'
            for profile in graphs:
                row[f'{profile}_nearest_distance_m']='';row[f'{profile}_connected_distance_m']=''
                row[f'{profile}_connected_status']='excluded_missing_coordinates'
            output.append(row);continue
        if not row['latitude'] or not row['longitude']:raise ValueError(f'Partial coordinates: {key}')
        point=Point(base.access.TO_METRES.transform(float(row['longitude']),float(row['latitude']),errcheck=True))
        if not area.covers(point):raise ValueError(f'Facility outside study polygon: {key}')
        row['qa_status']='evaluated'
        for profile,graph in graphs.items():
            hit,ties=nearest_matches(indices[profile],point)
            selected=states[profile][hit['key']]
            all_ties_disconnected=not any(states[profile][edge]['round_trip_topology'] for edge in ties)
            selected_bad=not selected['eligible_facility_target']
            prefix=f'{profile}_'
            row.update({prefix+'nearest_distance_m':f"{hit['distance']:.6f}",
                prefix+'nearest_way_id':graph.edges[hit['key']]['osmid'],
                prefix+'nearest_edge':core.json_text(hit['key']),
                prefix+'nearest_raw_wcc':selected['wcc_id'],
                prefix+'nearest_core_to_entry':selected['core_to_entry'],
                prefix+'nearest_exit_to_core':selected['exit_to_core'],
                prefix+'nearest_assignment_review':selected_bad,
                prefix+'all_co_nearest_edges_disconnected':all_ties_disconnected,
                prefix+'co_nearest_connected_alternative':any(states[profile][edge]['eligible_facility_target'] for edge in ties),
                prefix+'connected_distance_m':'',prefix+'connected_way_id':'',prefix+'connected_edge':'',
                prefix+'connected_status':'no_eligible_edge',prefix+'connected_search_covered':False,
                prefix+'connected_minus_nearest_m':'',prefix+'actual_vehicle_access':'unverified'})
            connected=qualified[profile]
            if connected is not None:
                admissible=connected.nearest(point);distance=admissible['distance']
                if distance<hit['distance']-1e-6:raise ValueError('Eligible subset is closer than full graph')
                row.update({prefix+'connected_distance_m':f'{distance:.6f}',
                    prefix+'connected_way_id':graph.edges[admissible['key']]['osmid'],
                    prefix+'connected_edge':core.json_text(admissible['key']),
                    prefix+'connected_status':'measured_within_saved_graph_extent',
                    prefix+'connected_search_covered':distance<point.distance(area.boundary)-1e-6,
                    prefix+'connected_minus_nearest_m':f"{distance-hit['distance']:.6f}"})
            if selected_bad:
                alerts.append(dict(시군명=key[0],시설일련번호=key[1],시설명=row['시설명'],profile=profile,
                    nearest_way_id=graph.edges[hit['key']]['osmid'],nearest_distance_m=row[prefix+'nearest_distance_m'],
                    connected_distance_m=row[prefix+'connected_distance_m'],
                    reasons=selected['rejection_reasons'],all_co_nearest_disconnected=all_ties_disconnected,
                    co_nearest_connected_alternative=row[prefix+'co_nearest_connected_alternative']))
        output.append(row)
    return output,alerts,eligibility_rows,core_info,states


def diagnose_way(way_id,roads,graphs,states,metadata,area):
    if way_id not in roads:return dict(status='way_not_in_preserved_source_census',way_id=way_id)
    target=roads[way_id];refs=target['nodes'];line=core.metric_line(target['coords'])
    shared={}
    for wid,road in roads.items():
        common=sorted(set(refs)&set(road['nodes']))
        if wid!=way_id and common:
            shared[str(wid)]={'shared_osm_node_ids':common,'tags':road['tags'],'rejections':road['rejections']}
    other_ids=[w for w in roads if w!=way_id]
    other_lines=[core.metric_line(roads[w]['coords']) for w in other_ids]
    tree=STRtree(other_lines) if other_lines else None
    node_checks=[]
    for n,coordinate in zip(refs,target['coords'],strict=True):
        point=Point(base.access.TO_METRES.transform(*coordinate,errcheck=True))
        entry=dict(osm_node_id=n,longitude=coordinate[0],latitude=coordinate[1],inside_study_area=area.covers(point),
            source_shared_way_ids=sorted(int(w) for w,d in shared.items() if n in d['shared_osm_node_ids']))
        if tree is not None:
            i=int(tree.nearest(point));entry.update(nearest_other_source_way=other_ids[i],distance_to_other_source_line_m=point.distance(other_lines[i]))
        node_checks.append(entry)
    crossings=[]
    if tree is not None:
        for i in tree.query(line,predicate='intersects'):
            w=other_ids[int(i)]
            if str(w) not in shared:crossings.append(w)
    profiles_result={}
    fixed_anchors,_=reference_core(graphs['drive_reference'])
    for profile,graph in graphs.items():
        edges=[(u,v,k) for u,v,k,d in graph.edges(keys=True,data=True) if d['osmid']==way_id]
        edge_rows=[states[profile][edge] for edge in edges]
        component_nodes=nx.node_connected_component(graph.to_undirected(as_view=True),edges[0][0]) if edges else set()
        component_ways=sorted({d['osmid'] for u,v,d in graph.edges(data=True) if u in component_nodes})
        exterior=metadata.get('graph_validation',{}).get(profile,{}).get('source_external_neighbors_at_retained_nodes',{})
        external={str(n):exterior[str(n)] for n in component_nodes if str(n) in exterior}
        active,_=active_reference_anchors(graph,fixed_anchors)
        weak_to_core=bool(component_nodes&active)
        filtered_frontier={}
        for wid,road in roads.items():
            contacts=sorted(set(road['nodes'])&component_nodes)
            if contacts and road['rejections'][profile]:
                filtered_frontier[str(wid)]={'source_shared_nodes':contacts,'tags':road['tags'],'rejections':road['rejections'][profile]}
        if not edges:classification='excluded_from_profile'
        elif not shared and all(c['inside_study_area'] for c in node_checks):classification='no_shared_highway_nodes_in_preserved_source'
        elif external:classification='outer_extent_external_connections_require_review'
        elif not weak_to_core and filtered_frontier:classification='disconnected_component_with_filtered_source_neighbors'
        elif not weak_to_core:classification='disconnected_source_component_from_reference_core'
        elif not all(r['round_trip_topology'] for r in edge_rows):classification='directional_connectivity_requires_review'
        else:classification='round_trip_topology_present'
        profiles_result[profile]=dict(classification=classification,original_directed_edges=edges,
            edge_connectivity=edge_rows,component_nodes=len(component_nodes),component_way_ids=component_ways,
            source_external_contacts=external,weak_connection_to_reference_core=weak_to_core,filtered_source_frontier=filtered_frontier)
    return dict(status='source_and_graphs_inspected',way_id=way_id,tags=target['tags'],
        original_node_order=refs,original_coordinates=target['coords'],filter_rejections=target['rejections'],
        source_shared_ways=shared,node_checks=node_checks,geometric_crossings_without_shared_node=sorted(crossings),
        profiles=profiles_result,scope='Preserved study-area way census and full selected way shapes; not a new nationwide PBF scan',
        caveat='No snapping or inferred connector; OSM topology does not establish actual passage or physical absence of a road')


def safe_input(folder,name):
    relative=Path(name)
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe artifact-relative path')
    path=(folder/relative).resolve(strict=True)
    if not path.is_relative_to(folder):raise ValueError('Artifact symlink escapes input directory')
    return path


def render_report(payload,metadata,rows,alerts,diagnosis):
    source=metadata['profile_source'];core_info=metadata['ordinary_reference_core']
    lines=['# 일반도로망 양방향 연결 접근거리 QA','',
        f"원본 PBF 기준 시각: {source['data_timestamp']}",f"원본 PBF SHA-256: {source['sha256']}",'',
        '저장된 세 도로망과 원본 지역 감사 자료만 읽었습니다. 전국 PBF 처리·새 도로 추출·원본 그래프 수정은 하지 않았습니다.',
        '단순 최근접 직선거리와 일반도로 기준망으로 진입·복귀 가능한 방향 간선까지의 최근접 직선거리를 구분합니다. 둘 다 주행거리는 아닙니다.',
        '기준망은 Drive 최대 WCC 안에서 일반도로 노드가 가장 많은 SCC를 택해 고정합니다. 각 profile에서 기준 anchor와 가장 많이 겹치는 단일 SCC를 선택합니다. 대상 u→v는 같은 기준 SCC→u와 v→기준 SCC가 모두 성립해야 합니다. motorway/trunk 및 링크는 시설 최근접 배정 대상에서 제외합니다.',
        f"고정 일반도로 anchor 노드: {core_info['ordinary_anchor_nodes']}개. WCC 소속이나 SCC 번호만으로 시설 통행을 승인하지 않습니다.",'',
        '| 모델 | 평가 시설 | 최근접 배정 검토 | 연결 적격 간선 없음 | 적격 검색 범위 충분성 미확인 |','|---|---:|---:|---:|---:|']
    for p in profiles.PROFILES:
        evaluated=[r for r in rows if r['qa_status']=='evaluated']
        lines.append(f"| {p} | {len(evaluated)} | {sum(bool(r[p+'_nearest_assignment_review']) for r in evaluated)} | {sum(r[p+'_connected_status']=='no_eligible_edge' for r in evaluated)} | {sum(not r[p+'_connected_search_covered'] for r in evaluated)} |")
    target=next(r for r in rows if base.access.identity(r)==TARGET_KEY)
    lines+=['','## 수정초등학교 강당 (청양군 / 18)','',
        '사용자 제공 관측: Drive 82.462631m, Drive+Service 72.665399m, way 1228606370, integrated_wcc_082, 일반도로 진입·복귀 False. 아래는 저장 자료를 직접 읽어 계산한 별도 QA 값입니다.',
        '| 모델 | 단순 최근접(m) | 양방향 연결 적격(m) | 단순 최근접 way | 검토 필요 |','|---|---:|---:|---|---|']
    for p in profiles.PROFILES:
        lines.append(f"| {p} | {target[p+'_nearest_distance_m']} | {target[p+'_connected_distance_m'] or '해당 간선 없음'} | {target[p+'_nearest_way_id']} | {target[p+'_nearest_assignment_review']} |")
    lines+=['',f"원본 way 조사 상태: {diagnosis['status']}."]
    labels={'excluded_from_profile':'필터/후보 정책으로 이 profile에서 제외됨',
        'no_shared_highway_nodes_in_preserved_source':'영역 내부의 원본 노드가 다른 highway와 공유되지 않음',
        'outer_extent_external_connections_require_review':'외곽에서 생략된 원본 연결이 있어 범위 확대 검토 필요',
        'disconnected_component_with_filtered_source_neighbors':'일반 기준망과 별도 WCC이며 필터 제외 원본 이웃이 있음; 복구 경로는 미확인',
        'disconnected_source_component_from_reference_core':'일반 기준망과 별도 WCC',
        'directional_connectivity_requires_review':'약연결 여부와 별개로 진입/복귀 방향성 검토 필요',
        'round_trip_topology_present':'OSM 노드 기준 양방향 도달성 있음; 차량 통행은 미확인'}
    for p,d in diagnosis.get('profiles',{}).items():
        lines.append(f"- {p}: {labels[d['classification']]}. 원본 방향 간선 {len(d['original_directed_edges'])}개, 해당 연결요소 노드 {d['component_nodes']}개.")
    lines+=['', '원본 공유 노드·필터 제외 연결 way·일방통행·외곽 접속·공간 교차와 노드 공유의 차이를 sujeong_way_diagnosis.json 및 sujeong_way_nodes.csv에 기록했습니다. 원본에 없는 연결선은 만들지 않았습니다.',
        '컴포넌트 ID는 그래프/단순화에 따라 달라질 수 있으므로 과거 integrated_wcc_082와 새 raw QA ID를 동일한 번호라고 가정하지 않습니다.',
        '회전 제한·barrier·조건부 통행·현장 출입구·차량 제원은 검토 대상입니다. 양방향 OSM 연결성도 실제 구호차량 통행을 확정하지 않습니다.',
        '좌표 미확보 대동마을회관 1개는 제외합니다. 적격 검색 거리가 시설→분석 외곽 거리보다 크면 실제 범위 밖의 더 가까운 도로 가능성이 있어 범위 충분성 미확인으로 표시합니다.',
        'facilities_connected_access_147.csv: 원본 시설 열과 세 모델별 두 거리 및 QA 상태. nearest_assignment_alerts.csv: 부적절한 단순 최근접 배정 탐지 목록.',
        '침수 분석·시설 수요 계산·창고 입지 최적화는 수행하지 않았습니다.']
    (payload/'qa_report_ko.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def build(root,artifact,destination):
    root=Path(root).resolve();artifact=Path(artifact).resolve(strict=True);destination=Path(destination).resolve()
    if destination.exists():raise FileExistsError(f'QA output exists: {destination}')
    if destination.is_relative_to(artifact) or artifact.is_relative_to(destination):raise ValueError('QA output must be separate from source artifact')
    if destination.is_relative_to(root) and not destination.is_relative_to(root/'output/road_network_qa'):
        raise ValueError('Repository QA outputs must use output/road_network_qa')
    preserved=base.input_hashes(root)
    raw_meta=safe_input(artifact,'metadata.json');metadata=json.loads(raw_meta.read_bytes())
    if metadata.get('status')!='data_validated':raise ValueError('Source profile data validation did not pass')
    required=['source_road_audit.jsonl','facilities_access_comparison_147.csv','source_node_tags.json','restriction_relations.json']
    required += [f'{p}/raw.graphml' for p in profiles.PROFILES]
    expected=metadata['output_sha256'];hashes={'metadata.json':file_hash(raw_meta)}
    for name in required:
        path=safe_input(artifact,name)
        if name not in expected or file_hash(path)!=expected[name]:raise ValueError(f'Artifact checksum mismatch: {name}')
        hashes[name]=expected[name]
    original,columns,_=base.load_facilities(root)
    saved,_=base.read_csv(artifact/'facilities_access_comparison_147.csv')
    if [{c:r[c] for c in columns} for r in saved]!=original:raise ValueError('Artifact facility inputs differ from preserved source CSV')
    area=base.load_area(root)
    if metadata['input_sha256'][str(base.AREA)]!=file_hash(root/base.AREA):raise ValueError('Artifact study-area checksum differs')
    roads={}
    with (artifact/'source_road_audit.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line);wid=row.pop('osm_way_id')
            if wid in roads:raise ValueError('Duplicate source way in artifact')
            if len(row['nodes'])<2 or len(row['nodes'])!=len(row['coords']):raise ValueError('Incomplete source geometry')
            roads[wid]=row
    graphs={}
    for profile in profiles.PROFILES:
        graph=ox.load_graphml(artifact/profile/'raw.graphml',edge_dtypes={'raw_segments':int,'length_5179':float})
        if graph.graph.get('source_sha256')!=metadata['source']['sha256']:raise ValueError('Profiles do not share the recorded PBF')
        if not graph.is_directed() or not graph.is_multigraph():raise ValueError('Expected directed multigraph')
        # Check each stored raw segment against source IDs, order, coordinates and tags.
        expected_segments=Counter()
        for wid,road in roads.items():
            if road['rejections'][profile]:continue
            one=core.is_oneway(road['tags'])
            reverse=one and ox.graph._is_path_reversed(road['tags'],{'-1','reverse','T'})
            for i,(u,v) in enumerate(zip(road['nodes'],road['nodes'][1:])):
                if u==v:continue
                a,b=(v,u) if reverse else (u,v)
                expected_segments[a,b,wid,i]+=1
                if not one:expected_segments[v,u,wid,i]+=1
        observed=Counter()
        for u,v,key,d in graph.edges(keys=True,data=True):
            wid=d['osmid'];road=roads[wid]
            if len(d['raw_segments'])!=1:raise ValueError('Expected an unsimplified source segment')
            i=d['raw_segments'][0];observed[u,v,wid,i]+=1
            a,b=road['nodes'][i:i+2];coords=road['coords'][i:i+2]
            if (u,v)==(b,a):coords=coords[::-1]
            if list(d['geometry'].coords)!=list(map(tuple,coords)):raise ValueError('Source segment geometry changed')
            if not base.saved_tags_match(d['osm_tags'],core.json_text(road['tags'])):raise ValueError('Source tags changed')
        if observed!=expected_segments:raise ValueError(f'Source directed segment coverage differs: {profile}')
        graphs[profile]=graph
    rows,alerts,edges,core_info,states=evaluate_facilities(original,graphs,area)
    prior_rows={base.access.identity(r):r for r in saved}
    for row in rows:
        previous=prior_rows[base.access.identity(row)]
        for profile in profiles.PROFILES:
            row[profile+'_prior_saved_wcc_id']=previous.get(profile+'_road_wcc_id','')
            row[profile+'_prior_saved_nearest_way_id']=previous.get(profile+'_road_edge_osmid','')
            if row['qa_status']=='evaluated':
                if abs(float(row[profile+'_nearest_distance_m'])-float(previous[profile+'_road_distance_m']))>2e-6:
                    raise ValueError('Recomputed unrestricted distance differs from saved profile result')
    if sum(r['qa_status']=='evaluated' for r in rows)!=146:raise ValueError('Expected 146 evaluated facilities')
    diagnosis=diagnose_way(TARGET_WAY,roads,graphs,states,metadata,area)
    node_tags=json.loads((artifact/'source_node_tags.json').read_bytes())
    for node in diagnosis.get('node_checks',[]):
        node['osm_node_tags']=core.json_text(node_tags.get(str(node['osm_node_id']),{}))
    related_ways={TARGET_WAY}
    for info in diagnosis.get('profiles',{}).values():related_ways.update(info['component_way_ids'])
    relations=json.loads((artifact/'restriction_relations.json').read_bytes())
    diagnosis['preserved_related_restriction_relations']=[r for r in relations if any(m['type']=='w' and m['ref'] in related_ways for m in r['members'])]
    diagnosis['restriction_relations_applied_to_routing']=False
    result=dict(status='qa_data_validated',created_at_utc=datetime.now(timezone.utc).isoformat(),
        profile_source=metadata['source'],artifact_input_sha256=hashes,reported_case=REPORTED_CASE,
        ordinary_reference_core=core_info,facilities_evaluated=146,excluded_missing_coordinates=1,
        alerts=len(alerts),national_pbf_opened=False,source_graphs_modified=False,vehicle_access_certified=False,
        control_source_geometry_and_directions_verified=True,unrestricted_distances_match_original_artifact=True,
        code_sha256=file_hash(Path(__file__)),preserved_input_sha256=preserved)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.connected-qa-',dir=destination.parent) as stage:
        payload=Path(stage)/'results';payload.mkdir()
        profiles.write_rows(payload/'facilities_connected_access_147.csv',rows)
        profiles.write_rows(payload/'nearest_assignment_alerts.csv',alerts,
            ['시군명','시설일련번호','시설명','profile','nearest_way_id','nearest_distance_m','connected_distance_m','reasons','all_co_nearest_disconnected','co_nearest_connected_alternative'])
        profiles.write_rows(payload/'edge_connected_eligibility.csv',edges)
        profiles.write_rows(payload/'sujeong_way_nodes.csv',diagnosis.get('node_checks',[]),
            ['osm_node_id','longitude','latitude','inside_study_area','source_shared_way_ids','nearest_other_source_way','distance_to_other_source_line_m','osm_node_tags'])
        write_json(payload/'sujeong_way_diagnosis.json',diagnosis)
        render_report(payload,result,rows,alerts,diagnosis)
        if any(file_hash(safe_input(artifact,n))!=h for n,h in hashes.items()):raise ValueError('Artifact inputs changed during QA')
        if base.input_hashes(root)!=preserved:raise ValueError('Repository inputs changed during QA')
        result['output_sha256']={p.name:file_hash(p) for p in payload.iterdir()}
        write_json(payload/'qa_metadata.json',result)
        if destination.exists():raise FileExistsError(f'QA output exists: {destination}')
        payload.rename(destination)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=build(ROOT,args.artifact_dir,args.output)
        print(core.json_text({'status':result['status'],'facilities_evaluated':146,'alerts':result['alerts'],'national_pbf_opened':False}))
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        print(f'Connected access QA failed: {exc}',file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
