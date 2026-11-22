"""Offline QA fixtures: distances are synthetic and never real facility findings."""
from pathlib import Path
import json
import socket
from unittest.mock import patch

import networkx as nx
import pytest
from shapely.geometry import Point,box

from scripts import connected_access_qa as qa
from scripts import integrated_road_graph as core
from scripts import compare_road_profiles as profiles
from scripts import build_integrated_road_network as base
from scripts.jukrim_pbf_audit import file_hash
from tests.test_integrated_road_network import write_pbf,wgs


def local_models():
    x,y=base.access.TO_METRES.transform(126.84,36.37)
    roads={
        100:dict(nodes=[1,2],coords=[wgs(x-30,y+50),wgs(x+30,y+50)],tags={'highway':'residential'}),
        qa.TARGET_WAY:dict(nodes=[3,4],coords=[wgs(x-20,y+5),wgs(x+20,y+5)],tags={'highway':'service'}),
        102:dict(nodes=[2,5],coords=[wgs(x+30,y+50),wgs(x+30,y+10)],tags={'highway':'service','oneway':'yes'}),
        103:dict(nodes=[6,7],coords=[wgs(x+15,y-30),wgs(x+15,y+80)],tags={'highway':'service'}),
    }
    area=box(x-500,y-500,x+500,y+500)
    drive=core.build_graphs(roads,core.drive_rejections(roads),area,{'county':area})[0]
    service=core.build_graphs(roads,core.network_rejections(roads,'drive_service'),area,{'county':area},network_type='drive_service')[0]
    return x,y,roads,area,drive,service


def test_island_and_ingress_only_service_are_not_eligible():
    x,y,roads,area,drive,service=local_models()
    anchors,_=qa.reference_core(drive)
    eligible,rows,_,_=qa.edge_eligibility(service,anchors)
    assert all(service.edges[e]['osmid']!=qa.TARGET_WAY for e in eligible)
    assert (2,5,0) not in eligible # main -> dead end, but no return
    one=next(r for r in rows if (r['u'],r['v'])==(2,5))
    assert one['core_to_entry'] and not one['exit_to_core']
    assert not nx.has_path(service,6,1) # geometric crossing is no OSM connection
    original=[{'시군명':'청양군','시설일련번호':'18','시설명':'synthetic','longitude':'126.84','latitude':'36.37'}]
    rows,alerts,_,_,states=qa.evaluate_facilities(original,dict(drive_reference=drive,drive_service=service,relief_vehicle_candidate=service),area)
    assert float(rows[0]['drive_service_nearest_distance_m'])==pytest.approx(5,abs=.02)
    assert float(rows[0]['drive_service_connected_distance_m'])==pytest.approx(50,abs=.02)
    assert rows[0]['drive_service_all_co_nearest_edges_disconnected']
    assert alerts
    for wid in roads:roads[wid]['rejections']={name:[] for name in profiles.PROFILES}
    diagnosis=qa.diagnose_way(qa.TARGET_WAY,roads,dict(drive_reference=drive,drive_service=service,relief_vehicle_candidate=service),states,{},area)
    assert diagnosis['profiles']['drive_service']['classification']=='no_shared_highway_nodes_in_preserved_source'
    assert 103 in diagnosis['geometric_crossings_without_shared_node']


def test_oneway_return_cycle_qualifies_and_controlled_road_does_not():
    x,y,roads,area,drive,service=local_models()
    anchors,_=qa.reference_core(drive)
    service.add_edge(5,1,osmid=200,highway='service')
    service.add_edge(1,2,osmid=201,highway='motorway')
    eligible,rows,_,_=qa.edge_eligibility(service,anchors)
    assert (2,5,0) in eligible # reverse edge unnecessary if a real return cycle exists
    controlled=next(r for r in rows if r['osm_way_id']==201)
    assert controlled['round_trip_topology'] and not controlled['eligible_facility_target']


def test_missing_reference_core_fails_explicitly():
    graph=nx.MultiDiGraph();graph.add_edge(1,2,highway='motorway')
    with pytest.raises(ValueError,match='No strongly connected ordinary-road'):qa.reference_core(graph)


def test_full_147_fixture_artifact_qa_readonly_and_corruption(tmp_path):
    original,columns,prior=base.load_facilities(base.ROOT)
    nodes={};ways={}
    # One real-reference-style ordinary ring connects every synthetic facility location.
    ring=[]
    for i,row in enumerate(original,1):
        if not row['latitude']:continue
        x,y=base.access.TO_METRES.transform(float(row['longitude']),float(row['latitude']))
        n=i*10;ring.append(n)
        nodes[n]=wgs(x,y+80);nodes[n+1]=wgs(x-10,y+4);nodes[n+2]=wgs(x+10,y+4)
        ways[i]=([n+1,n+2],{'highway':'service'})
    ways[9999]=(ring+[ring[0]],{'highway':'residential'})
    pbf=tmp_path/'synthetic.osm.pbf';write_pbf(pbf,nodes,ways)
    artifact=tmp_path/'original-artifact'
    profiles.build(base.ROOT,pbf,artifact,checksum=file_hash(pbf),source='synthetic fixture only')
    before={str(p.relative_to(artifact)):file_hash(p) for p in artifact.rglob('*') if p.is_file()}
    out=tmp_path/'qa'
    def deny(*a,**k):raise AssertionError('QA must not use network')
    with patch.object(socket.socket,'connect',deny),patch('socket.getaddrinfo',deny):
        result=qa.build(base.ROOT,artifact,out)
    assert result['facilities_evaluated']==146 and result['national_pbf_opened'] is False
    assert before=={str(p.relative_to(artifact)):file_hash(p) for p in artifact.rglob('*') if p.is_file()}
    rows,_=base.read_csv(out/'facilities_connected_access_147.csv')
    assert [{c:r[c] for c in columns} for r in rows]==original
    assert len([r for r in rows if r['qa_status']=='excluded_missing_coordinates'])==1
    assert all(float(r['drive_service_connected_distance_m'])>=float(r['drive_service_nearest_distance_m']) for r in rows if r['qa_status']=='evaluated')
    assert all(file_hash(out/name)==digest for name,digest in result['output_sha256'].items())
    assert '수정초등학교' in (out/'qa_report_ko.md').read_text()
    with pytest.raises(FileExistsError):qa.build(base.ROOT,artifact,out)
    with (artifact/'source_road_audit.jsonl').open('a') as f:f.write('corruption')
    with pytest.raises(ValueError,match='checksum mismatch'):qa.build(base.ROOT,artifact,tmp_path/'must-not-publish')
    assert not (tmp_path/'must-not-publish').exists()


def test_candidate_split_anchors_cannot_create_false_round_trip():
    drive=nx.MultiDiGraph()
    for u,v in [(1,2),(2,3),(3,4)]:
        drive.add_edge(u,v,highway='residential',osmid=1)
        drive.add_edge(v,u,highway='residential',osmid=1)
    anchors,_=qa.reference_core(drive)
    candidate=drive.copy();candidate.remove_edge(2,1)
    eligible,rows,_,_=qa.edge_eligibility(candidate,anchors)
    # Node 1 was a reference anchor, but it is no longer in the candidate core SCC.
    assert (1,2,0) not in eligible
    assert not next(r for r in rows if r['u']==1)['core_to_entry']


def test_equal_distance_connected_alternative_is_kept_separate():
    x,y=base.access.TO_METRES.transform(126.84,36.37)
    coords=[wgs(x-20,y+20),wgs(x+20,y+20)]
    roads={100:dict(nodes=[10,20],coords=coords,tags={'highway':'residential'}),
           101:dict(nodes=[1,2],coords=coords,tags={'highway':'service'})}
    area=box(x-100,y-100,x+100,y+100)
    drive=core.build_graphs(roads,core.drive_rejections(roads),area,{'a':area})[0]
    service=core.build_graphs(roads,core.network_rejections(roads,'drive_service'),area,{'a':area},network_type='drive_service')[0]
    source=[{'시군명':'청양군','시설일련번호':'18','시설명':'synthetic','longitude':'126.84','latitude':'36.37'}]
    rows,alerts,_,_,_=qa.evaluate_facilities(source,dict(drive_reference=drive,drive_service=service,relief_vehicle_candidate=service),area)
    row=rows[0]
    assert row['drive_service_nearest_assignment_review']
    assert row['drive_service_co_nearest_connected_alternative']
    assert not row['drive_service_all_co_nearest_edges_disconnected']
    assert row['drive_service_connected_distance_m']==row['drive_service_nearest_distance_m']
    assert alerts


def test_no_eligible_edge_is_not_replaced_with_zero():
    x,y,roads,area,drive,service=local_models()
    controlled=drive.copy()
    for _,_,d in controlled.edges(data=True):d['highway']='motorway'
    source=[{'시군명':'청양군','시설일련번호':'18','시설명':'synthetic','longitude':'126.84','latitude':'36.37'}]
    rows,_,_,_,_=qa.evaluate_facilities(source,dict(drive_reference=drive,drive_service=service,relief_vehicle_candidate=controlled),area)
    assert rows[0]['relief_vehicle_candidate_connected_status']=='no_eligible_edge'
    assert rows[0]['relief_vehicle_candidate_connected_distance_m']==''
