"""Synthetic/offline tests only. No nationwide data or network access."""
import json
import socket
from pathlib import Path
from unittest.mock import patch

import networkx as nx
import osmnx as ox
import osmium
import pytest
from shapely.geometry import box

from scripts import build_integrated_road_network as base
from scripts import integrated_road_graph as core
from scripts import compare_road_profiles as profiles
from scripts.road_vehicle_policy import vehicle_policy
from scripts.jukrim_pbf_audit import file_hash
from tests.test_integrated_road_network import write_pbf, wgs


def test_exact_filters_and_vehicle_access_hierarchy():
    roads={1:dict(tags={'highway':'service'}),2:dict(tags={'highway':'service','service':'parking_aisle'}),3:dict(tags={'highway':'track'}),4:dict(tags={'highway':'service','access':'private'})}
    drive=core.network_rejections(roads,'drive');service=core.network_rejections(roads,'drive_service')
    assert drive[1] and not service[1]
    assert all(service[i] for i in (2,3,4))
    assert vehicle_policy({'access':'no','motor_vehicle':'yes'})['candidate_included']
    assert not vehicle_policy({'access':'yes','vehicle':'no'})['candidate_included']
    assert not vehicle_policy({'motor_vehicle':'no','motor_vehicle:conditional':'yes @ (emergency)'})['candidate_included']
    assert not vehicle_policy({'hgv':'no','motorcar':'yes'})['candidate_included']
    policy=vehicle_policy({'maxweight':'3.5','surface':'unpaved','bridge':'yes'},[{'barrier':'gate'}])
    assert policy['review_required'] and not policy['vehicle_passage_confirmed']
    assert 'source_node_barrier_or_access_restriction' in policy['review_reasons']
    assert 'vehicle_dimensions_and_load_not_compared' in policy['review_reasons']


def test_service_shared_nodes_and_oneway_without_geometric_connection(tmp_path):
    x,y=base.access.TO_METRES.transform(126.84,36.37)
    area=box(x-500,y-500,x+500,y+500)
    roads={
        1:dict(nodes=[1,2],coords=[wgs(x-100,y),wgs(x+100,y)],tags={'highway':'residential'}),
        2:dict(nodes=[2,3,4],coords=[wgs(x+100,y),wgs(x+100,y+50),wgs(x+100,y+100)],tags={'highway':'service','oneway':'yes'}),
        3:dict(nodes=[5,6],coords=[wgs(x,y-100),wgs(x,y+100)],tags={'highway':'service'}),
    }
    rejected=core.network_rejections(roads,'drive_service')
    raw,simple,checks=core.build_graphs(roads,rejected,area,{'county':area},network_type='drive_service')
    assert raw.has_edge(2,3) and not raw.has_edge(3,2)
    assert not nx.has_path(raw,5,1)
    drive,_,_=core.build_graphs(roads,core.drive_rejections(roads),area,{'county':area})
    connection=profiles.anchor_reachability(raw,drive)
    topo=profiles.service_topology(2,roads,raw,connection,profiles.Point(x+100,y+70))
    assert topo['ordinary_to_service'] and not topo['service_to_ordinary']
    assert checks['retained_node_wcc_and_scc_partitions_preserved']
    p={w:vehicle_policy(r['tags']) for w,r in roads.items()}
    rej={n:rejected for n in profiles.PROFILES}
    profiles.add_identity(raw,roads,'a'*64,'drive_service',p,rej)
    profiles.add_identity(simple,roads,'a'*64,'drive_service',p,rej)
    raw_ids={d['source_segment_ids'] for _,_,d in raw.edges(data=True)}
    assert raw_ids=={i for _,_,d in simple.edges(data=True) for i in d['source_segment_ids'].split(';')}


def test_synthetic_three_profiles_full_pipeline_keeps_all_facility_fields(tmp_path):
    original,columns,prior=base.load_facilities(base.ROOT)
    nodes={};ways={}
    for i,row in enumerate(original,1):
        if not row['latitude']:continue
        x,y=base.access.TO_METRES.transform(float(row['longitude']),float(row['latitude']))
        n=i*10
        for j,(dx,dy) in enumerate([(-20,30),(0,30),(20,30),(0,3),(0,0)]):nodes[n+j]=wgs(x+dx,y+dy)
        ways[i*10]=([n,n+1,n+2],{'highway':'residential'})
        ways[i*10+1]=([n+1,n+3,n+4],{'highway':'service','surface':'asphalt'})
        if i%10==0:ways[i*10+1][1].update(hgv='no',**{'hgv:conditional':'yes @ (delivery)'})
    pbf=tmp_path/'synthetic.osm.pbf'
    header=osmium.io.Header();header.set('osmosis_replication_timestamp','2026-01-01T00:00:00Z')
    with osmium.SimpleWriter(pbf,header=header) as writer:
        for node,coordinate in sorted(nodes.items()):
            writer.add_node(osmium.osm.mutable.Node(id=node,location=coordinate,tags={'barrier':'gate'} if node==min(nodes) else {}))
        for way,(refs,attrs) in sorted(ways.items()):writer.add_way(osmium.osm.mutable.Way(id=way,nodes=refs,tags=attrs))
        writer.add_relation(osmium.osm.mutable.Relation(id=501,tags={'type':'restriction','restriction':'no_right_turn'},members=[('w',10,'from'),('n',11,'via'),('w',11,'to')]))
    before=base.input_hashes(base.ROOT);out=tmp_path/'results'
    def deny(*a,**k):raise AssertionError('Network forbidden during synthetic analysis')
    with patch.object(socket.socket,'connect',deny),patch('socket.getaddrinfo',deny):
        meta=profiles.build(base.ROOT,pbf,out,checksum=file_hash(pbf),source='synthetic test only')
    assert base.input_hashes(base.ROOT)==before
    assert json.loads((out/'restriction_relations.json').read_text())[0]['id']==501
    assert json.loads((out/'source_node_tags.json').read_text())[str(min(nodes))]['barrier']=='gate'
    assert meta['status']=='data_validated' and meta['verification']['same_pbf_for_all_profiles']
    assert not meta['prior_run_comparison']['same_snapshot']
    assert meta['facility_summary']['service_vs_drive']['decreased']==146
    for name in profiles.PROFILES:
        assert (out/f'{name}.graphml').exists()
        assert (out/name/'raw.graphml').exists()
        assert meta['facility_summary'][name]['matched']==146
    rows,_=base.read_csv(out/'facilities_access_comparison_147.csv')
    assert [{k:r[k] for k in columns} for r in rows]==original
    excluded=[r for r in rows if r['drive_service_road_access_status']!='matched']
    assert len(excluded)==1 and excluded[0]['시설명']=='대동마을회관'
    assert excluded[0]['drive_service_road_distance_m']==''
    details,_=base.read_csv(out/'remaining_outliers_4.csv')
    assert {r['시설일련번호'] for r in details}==profiles.OUTLIERS
    services,_=base.read_csv(out/'service_road_audit.csv')
    assert any(r['candidate_included']=='False' for r in services)
    assert all(file_hash(out/p)==h for p,h in meta['output_sha256'].items())
    trace,_=base.read_csv(out/'source_segment_correspondence.csv')
    ids={n:{r['source_segment_id'] for r in trace if r['profile']==n} for n in profiles.PROFILES}
    assert ids['drive_reference']<=ids['drive_service']
    assert ids['relief_vehicle_candidate']<=ids['drive_service']
    with pytest.raises(FileExistsError):profiles.build(base.ROOT,pbf,out,checksum=file_hash(pbf),source='synthetic')


@pytest.mark.parametrize('failure',['bad_sha','missing_ref'])
def test_new_pipeline_failure_never_publishes_results(tmp_path,failure):
    pbf=tmp_path/'bad.osm.pbf'
    write_pbf(pbf,{1:(126.84,36.37)},{1:([1,999],{'highway':'service'})})
    destination=tmp_path/'results'
    checksum='0'*64 if failure=='bad_sha' else file_hash(pbf)
    with pytest.raises(ValueError):profiles.build(base.ROOT,pbf,destination,checksum=checksum,source='synthetic')
    assert not destination.exists()


@pytest.mark.parametrize('mode',['same','latest','transient','corrupt'])
def test_profile_download_selects_one_snapshot_and_never_retries(tmp_path,monkeypatch,mode):
    from collections import deque
    from scripts import integrated_pbf_source as pbf_source
    from tests.test_integrated_road_network import PBFResponse,mock_geofabrik
    pbf=tmp_path/'fixture.osm.pbf'
    write_pbf(pbf,{1:(126.84,36.37),2:(126.85,36.37)},{1:([1,2],{'highway':'residential'})})
    prior={'url':profiles.ARCHIVE_URL,'sha256':file_hash(pbf),'data_timestamp':'2026-01-01T00:00:00Z'}
    payload=pbf.read_bytes()
    if mode in ('latest','transient'):responses=deque([PBFResponse(503 if mode=='transient' else 404),PBFResponse(200,file_hash(pbf,'md5').encode()),PBFResponse(200,payload)])
    else:responses=deque([PBFResponse(200,payload)])
    calls,delays=mock_geofabrik(monkeypatch,responses)
    if mode=='corrupt':
        prior['sha256']='0'*64
        with pytest.raises(ValueError,match='no latest fallback'):pbf_source.acquire_profile_pbf(tmp_path,prior,{})
    else:
        path,metadata=pbf_source.acquire_profile_pbf(tmp_path,prior,{})
        assert path.read_bytes()==payload and metadata['sha256']==file_hash(pbf)
        assert metadata['selection']==('prior_run_archive' if mode=='same' else 'latest_after_prior_run_unavailable')
    assert delays==[]
    assert calls==([profiles.ARCHIVE_URL,pbf_source.URL+'.md5',pbf_source.URL] if mode in ('latest','transient') else [profiles.ARCHIVE_URL])


def test_preserved_real_jukrim_all_profile_links_remain_bidirectional():
    evidence=json.loads((base.ROOT/base.REFERENCE).read_bytes())
    roads={r['way_id']:dict(nodes=r['nodes'],coords=r['geometry']['coordinates'],tags=r['tags']) for r in evidence['roads']}
    area=base.load_area(base.ROOT);counties=base.load_counties(base.ROOT)
    for profile in ('drive','drive_service'):
        raw,simple,check=core.build_graphs(roads,core.network_rejections(roads,profile),area,counties,network_type=profile)
        for graph in (raw,simple):
            links=core.jukrim_links(graph)
            assert all(links['edges_by_way'].values())
            assert all(links['directed_reachability'].values())
        assert check['original_directed_segments_preserved']
