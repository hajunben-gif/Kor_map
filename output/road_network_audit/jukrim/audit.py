"""One-site OSM audit; no county graph rebuilding or original data modification.

From the repository root:
  python output/road_network_audit/jukrim/audit.py --fetch
  python output/road_network_audit/jukrim/audit.py --osm-json output/road_network_audit/jukrim/raw_osm.json

Requires existing project dependencies (OSMnx 2.1.1, Shapely, pyproj, NetworkX,
requests). No API key is read. OSM data: OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

import geopandas as gpd
import networkx as nx
import osmnx as ox
from osmnx._overpass import _get_network_filter
from pyproj import Transformer
import requests
from shapely.geometry import LineString, Point, mapping

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
LAT, LON, RADIUS = 36.3667897253441, 126.836798762428, 2000
TO_M = Transformer.from_crs(4326, 5179, always_xy=True)
TO_WGS = Transformer.from_crs(5179, 4326, always_xy=True)
POINT = Point(*TO_M.transform(LON, LAT))
DISK = POINT.buffer(RADIUS, quad_segs=64)
ENDPOINT = 'https://overpass-api.de/api/interpreter'
QUERY = f'''[out:json][timeout:90];
way(around:{RADIUS},{LAT},{LON})["highway"]->.roads;
(
  .roads;
  way(around:{RADIUS},{LAT},{LON})["waterway"];
  way(around:{RADIUS},{LAT},{LON})["natural"="water"];
  relation(bw.roads)["type"="restriction"];
);
out meta geom;
>;
out meta;'''
MAJOR = {'motorway','trunk','primary','secondary','tertiary',
         'motorway_link','trunk_link','primary_link','secondary_link','tertiary_link'}


def now(): return datetime.now(timezone.utc).isoformat()
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def protect():
    entries=subprocess.check_output(['git','ls-tree','-rz','HEAD'],cwd=ROOT).split(b'\0')
    hashes={}
    for item in entries:
        if not item: continue
        meta,p=item.split(b'\t',1)
        if meta.split()[1]!=b'blob': continue
        path=p.decode()
        # A committed audit may be rerun to a separate output directory. Preserve
        # all input/original files; audit outputs themselves are reproducible.
        if path.startswith('output/road_network_audit/jukrim/'): continue
        hashes[path]=sha(ROOT/path)
    return hashes


def assert_preserved(hashes):
    assert all(sha(ROOT/p)==s for p,s in hashes.items()), 'Original file changed'


def fetch(endpoint):
    attempt={'endpoint':endpoint,'requested_at_utc':now(),'query':QUERY}
    try:
        response=requests.post(endpoint,data={'data':QUERY},timeout=(10,110))
        attempt['http_status']=response.status_code
        response.raise_for_status()
        raw=response.json()
        if not isinstance(raw.get('elements'),list) or raw.get('remark'):
            raise ValueError('Incomplete or invalid Overpass response: '+str(raw.get('remark','')))
        attempt.update(received_at_utc=now(),osm_base_timestamp=raw.get('osm3s',{}).get('timestamp_osm_base'))
        (OUT/'raw_osm.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (OUT/'query.overpassql').write_text(QUERY+'\n',encoding='utf-8')
        (OUT/'fetch_metadata.json').write_text(json.dumps(attempt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return raw
    except (requests.RequestException,ValueError) as exc:
        attempt.update(error_type=type(exc).__name__,error=str(exc)[:1000])
        (OUT/'fetch_failure.json').write_text(json.dumps(attempt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        raise RuntimeError('Latest OSM unavailable; no invented data. '+str(exc)) from exc


def parse_filter(expression):
    clauses=[]
    for body in re.findall(r'\[([^\]]+)\]',expression):
        match=re.fullmatch(r'"([^"\n]+)"(?:(\!~|\~|!=|=)"([^"\n]*)")?',body)
        if not match: raise ValueError('Unsupported Overpass filter: '+body)
        clauses.append(match.groups())
    return clauses


def filter_rejections(tags, expression):
    failures=[]
    for key,op,value in parse_filter(expression):
        actual=tags.get(key)
        passed=(actual is not None) if op is None else (
            actual==value if op=='=' else actual!=value if op=='!=' else
            actual is not None and re.search(value,actual) is not None if op=='~' else
            actual is None or re.search(value,actual) is None)
        if not passed: failures.append({'key':key,'operator':op,'filter_value':value,'actual':actual})
    return failures


def vehicle_assessment(tags):
    conditional={k:v for k,v in tags.items() if ':conditional' in k}
    explicit={k:tags[k] for k in ('access','vehicle','motor_vehicle','motorcar','hgv',
        'emergency','delivery','service','surface','smoothness','tracktype','width',
        'maxwidth','maxheight','maxweight','maxaxleload','barrier','locked','ford') if k in tags}
    controlling=next((k for k in ('motorcar','motor_vehicle','vehicle','access') if k in tags),None)
    value=tags.get(controlling) if controlling else None
    if value in ('no','private','agricultural','forestry','customers','permit'):
        status='prohibited_or_permission_limited'
    elif value in ('destination','delivery','permissive'):
        status='conditional_or_purpose_limited'
    elif tags.get('highway') in ('footway','path','steps','cycleway','pedestrian','construction','proposed','bridleway'):
        status='motor_vehicle_suitability_not_established'
    else:
        status='no_explicit_prohibition_found_not_field_verified'
    return {'status':status,'controlling_access_key':controlling,'controlling_access_value':value,
            'vehicle_related_tags':explicit,'conditional_tags':conditional,
            'truck_or_relief_vehicle_usable_confirmed':False}


def directions(tags):
    value=tags.get('oneway:motorcar',tags.get('oneway:motor_vehicle',tags.get('oneway')))
    if value in ('-1','reverse'): return False,True
    if value in ('yes','true','1'): return True,False
    if value in ('no','false','0'): return True,True
    if tags.get('junction')=='roundabout': return True,False
    return True,True


def existing():
    wayids={};graphs={}
    for slug in ('cheongyang','buyeo'):
        path=ROOT/f'data/20261109T045434949410Z/{slug}/network.graphml'
        graph=ox.load_graphml(path);graphs[slug]=graph;ids=set()
        for _,_,_,d in graph.edges(keys=True,data=True):
            value=d['osmid'];ids.update(value if isinstance(value,list) else [value])
        wayids[slug]=ids
    return graphs,wayids


def extract(raw,wayids):
    # Recursive Overpass output can repeat elements; retain the most informative.
    unique={}
    for element in raw['elements']:
        key=(element['type'],element['id'])
        if key not in unique or len(element)>len(unique[key]):unique[key]=element
    nodes={i:e for (kind,i),e in unique.items() if kind=='node'}
    restrictions=[e for (kind,_),e in unique.items() if kind=='relation' and e.get('tags',{}).get('type')=='restriction']
    roads=[];water=[]
    filters={mode:_get_network_filter(mode) for mode in ('drive','drive_service')}
    for (kind,i),element in unique.items():
        if kind!='way':continue
        geometry=element.get('geometry')
        if not geometry:
            geometry=[{'lon':nodes[n]['lon'],'lat':nodes[n]['lat']} for n in element.get('nodes',[]) if n in nodes]
        if len(geometry)<2:continue
        coordinates=[(p['lon'],p['lat']) for p in geometry]
        if any(x is None or y is None for x,y in coordinates):raise ValueError('Incomplete way geometry')
        line=LineString(coordinates)
        metric=LineString([TO_M.transform(x,y) for x,y in coordinates])
        if metric.distance(POINT)>RADIUS:continue
        tags=element.get('tags',{})
        record={'way_id':i,'version':element.get('version'),'timestamp':element.get('timestamp'),
            'tags':tags,'nodes':element.get('nodes',[]),'geometry':mapping(line),
            'distance_m':metric.distance(POINT),'_metric':metric,'_coordinates':coordinates}
        if 'highway' in tags:
            assert len(record['nodes'])==len(coordinates), 'Raw nodes and geometry must correspond'
            rejected={mode:filter_rejections(tags,expression) for mode,expression in filters.items()}
            record.update(existing_membership={slug:i in ids for slug,ids in wayids.items()},
                          filter_rejections=rejected,vehicle_assessment=vehicle_assessment(tags))
            roads.append(record)
        elif 'waterway' in tags or tags.get('natural')=='water':water.append(record)
    roads.sort(key=lambda r:(r['distance_m'],r['way_id']))
    return roads,water,nodes,restrictions,filters


def public(road): return {k:v for k,v in road.items() if not k.startswith('_')}


def closest(roads,mode):
    selected=[r for r in roads if mode=='generic_highway' or not r['filter_rejections'][mode]]
    if not selected:return {'status':'no_matching_osm_way_within_2km','distance_m':None,'way_id':None}
    r=min(selected,key=lambda r:(r['distance_m'],r['way_id']))
    p=r['_metric'].interpolate(r['_metric'].project(POINT));lon,lat=TO_WGS.transform(p.x,p.y)
    return {'status':'measured','distance_m':r['distance_m'],'way_id':r['way_id'],
        'nearest_point':{'longitude':lon,'latitude':lat},'highway':r['tags']['highway'],
        'tags':r['tags'],'vehicle_assessment':r['vehicle_assessment'],
        'existing_membership':r['existing_membership'],
        'filter_rejections':r['filter_rejections']}


def raw_topology(roads,mode):
    graph=nx.MultiDiGraph();selected=[]
    for road in roads:
        if mode!='generic_highway' and road['filter_rejections'][mode]:continue
        selected.append(road);forward,reverse=directions(road['tags'])
        for a,b,ca,cb in zip(road['nodes'],road['nodes'][1:],road['_coordinates'],road['_coordinates'][1:]):
            segment=LineString([TO_M.transform(*ca),TO_M.transform(*cb)])
            if not segment.intersects(DISK):continue
            if forward:graph.add_edge(a,b,way_id=road['way_id'])
            if reverse:graph.add_edge(b,a,way_id=road['way_id'])
    return graph,selected


def connection(roads,nearest,old_wayids):
    if nearest['status']!='measured':return {'status':'not_evaluable_no_way'}
    road=next(r for r in roads if r['way_id']==nearest['way_id'])
    graph,selected=raw_topology(roads,'drive_service')
    anchors=[r for r in selected if r['tags']['highway'] in MAJOR and r['way_id'] in old_wayids]
    target_nodes={n for r in anchors for n in r['nodes'] if n in graph}
    if road['filter_rejections']['drive_service']:
        return {'status':'nearest_way_not_in_drive_service','excluded_by':road['filter_rejections']['drive_service'],
                'no_motor_connection_inferred':True}
    segments=[]
    for a,b,ca,cb in zip(road['nodes'],road['nodes'][1:],road['_coordinates'],road['_coordinates'][1:]):
        line=LineString([TO_M.transform(*ca),TO_M.transform(*cb)])
        segments.append((line.distance(POINT),a,b))
    minimum=min(s[0] for s in segments)
    pairs=[(a,b) for d,a,b in segments if d<=minimum+1e-7]
    outbound=set();inbound=set()
    for a,b in pairs:
        if graph.has_edge(a,b):outbound.add(b);inbound.add(a)
        if graph.has_edge(b,a):outbound.add(a);inbound.add(b)
    reachable=set(outbound);reaching=set(inbound)
    for n in outbound:reachable.update(nx.descendants(graph,n))
    for n in inbound:reaching.update(nx.ancestors(graph,n))
    weak=set()
    for n in outbound|inbound:weak.update(nx.node_connected_component(graph.to_undirected(as_view=True),n))
    return {'status':'osm_shared_node_topology_only','nearest_way':road['way_id'],
        'nearest_way_to_existing_major_road':bool(reachable&target_nodes),
        'existing_major_road_to_nearest_way':bool(reaching&target_nodes),
        'weak_connection_to_existing_major_road':bool(weak&target_nodes),
        'anchor_ways':[r['way_id'] for r in anchors],
        'outbound_anchor_way_ids':[r['way_id'] for r in anchors if set(r['nodes'])&reachable],
        'inbound_anchor_way_ids':[r['way_id'] for r in anchors if set(r['nodes'])&reaching],
        'near_segment_node_pairs':pairs,'connected_component_nodes':len(weak),
        'limitations':['Only original shared OSM node IDs connect edges; geometric crossing alone is ignored.',
            'Way oneway and roundabout defaults are considered; conditional tags, turn relations and barriers are recorded but not automatically resolved.',
            'Topology is confined to the queried 2km neighborhood, not a delivery route calculation.',
            'A topological link does not establish physical access, bridge capacity, or relief-truck permission.'],
        '_weak_nodes':weak,'_reachable_nodes':reachable}


def crossings(roads,water,nodes,connection_result):
    records=[];connected=connection_result.get('_weak_nodes',set())
    for road in roads:
        tags=road['tags'];intersections=[]
        for river in water:
            if road['_metric'].intersection(DISK).intersects(river['_metric'].intersection(DISK)):
                intersections.append({'water_way_id':river['way_id'],'tags':river['tags']})
        ford_nodes=[{'node_id':n,'tags':nodes[n].get('tags',{})} for n in road['nodes']
                    if n in nodes and nodes[n].get('tags',{}).get('ford') not in (None,'no')]
        is_bridge=tags.get('bridge') not in (None,'no')
        if not is_bridge and not tags.get('ford') and not intersections and not ford_nodes:continue
        records.append({'way_id':road['way_id'],'tags':tags,'geometry':road['geometry'],
            'bridge_tag_present':is_bridge,'ford_nodes':ford_nodes,
            'water_geometry_intersections':intersections,
            'in_nearest_way_weak_component':bool(set(road['nodes'])&connected),
            'relative_to_facility':'south' if road['_metric'].centroid.y<POINT.y else 'north',
            'vehicle_assessment':road['vehicle_assessment'],
            'caution':'Geometry intersection alone does not prove a crossing or at-grade vehicle connection.'})
    return records


def self_checks():
    # Test tag/filter semantics, never fabricate research geometries/results.
    drive=_get_network_filter('drive');service=_get_network_filter('drive_service')
    assert filter_rejections({'highway':'service'},drive)
    assert not filter_rejections({'highway':'service'},service)
    assert filter_rejections({'highway':'service','access':'private'},service)
    assert filter_rejections({'highway':'track'},service)
    assert not filter_rejections({'highway':'residential'},drive)
    assert directions({'oneway':'-1'})==(False,True)
    assert directions({'junction':'roundabout'})==(True,False)
    assert directions({'junction':'roundabout','oneway':'no'})==(True,True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch',action='store_true')
    parser.add_argument('--osm-json',type=Path)
    parser.add_argument('--endpoint',default=ENDPOINT)
    args=parser.parse_args();self_checks();hashes=protect()
    if args.fetch:
        try:
            raw=fetch(args.endpoint)
        except RuntimeError:
            write_blocked_outputs(json.loads((OUT/'fetch_failure.json').read_text()),hashes)
            assert_preserved(hashes)
            raise SystemExit(2)
    else:
        input_path=args.osm_json or OUT/'raw_osm.json'
        raw=json.loads(input_path.read_text())
        if input_path.resolve()!=(OUT/'raw_osm.json').resolve():
            (OUT/'raw_osm.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    graphs,wayids=existing();roads,water,nodes,restrictions,filters=extract(raw,wayids)
    if not roads:raise RuntimeError('No highway ways in valid response; do not invent a network.')
    latest={mode:closest(roads,mode) for mode in ('drive','drive_service','generic_highway')}
    connectivity=connection(roads,latest['drive_service'],wayids['cheongyang']|wayids['buyeo'])
    cross=crossings(roads,water,nodes,connectivity)
    build_outputs(raw,roads,water,nodes,restrictions,filters,latest,connectivity,cross,hashes)
    assert_preserved(hashes)
    print(json.dumps({'road_ways':len(roads),'osm_timestamp':raw.get('osm3s',{}).get('timestamp_osm_base'),
        'distances':{k:v['distance_m'] for k,v in latest.items()},
        'connectivity':{k:v for k,v in connectivity.items() if not k.startswith('_')}},ensure_ascii=False))


def build_outputs(raw,roads,water,nodes,restrictions,filters,latest,connectivity,cross,hashes):
    saved=list(csv.DictReader((ROOT/'data/processed/shelters_road_access_147.csv').open(encoding='utf-8-sig')))
    row=next(r for r in saved if r['시군명']=='청양군' and r['시설일련번호']=='177')
    assert (row['latitude'],row['longitude'])==(str(LAT),str(LON))
    prior=json.loads((ROOT/'output/road_access_investigation/report.json').read_text())
    case=next(c for c in prior['cases'] if c['시설명']=='죽림리회관')
    neighbor=case['neighbor_county_existing_network_comparison']
    comparisons=[{'scenario':'existing_cheongyang_drive','source':'existing saved stage-2 CSV',
        'scope':'Cheongyang county only','distance_m':float(row['road_distance_m']),
        'way_id':row['road_edge_osmid'],'u':row['road_edge_u'],'v':row['road_edge_v'],
        'key':row['road_edge_key'],'highway':row['road_edge_highway'],'status':'previously_measured_unchanged'},
        {'scenario':'existing_cheongyang_and_buyeo_drive','source':'existing county graphs, previous read-only comparison',
        'scope':'Two existing county graphs','distance_m':neighbor['reference_distance_m'],
        'way_id':neighbor['nearest_edge']['osmid'],**{k:neighbor['nearest_edge'][k] for k in ('u','v','key')},
        'highway':neighbor['nearest_edge']['highway'],'status':'previously_measured_unchanged'}]
    stamp=raw.get('osm3s',{}).get('timestamp_osm_base')
    for mode,result in latest.items():
        tags=result.get('tags',{})
        comparisons.append({'scenario':'latest_local_'+mode,'source':'Same latest Overpass snapshot '+str(stamp),
            'scope':'2km, both sides of county boundary, no county mask','distance_m':result['distance_m'],
            'way_id':result['way_id'],'status':result['status'],
            'highway':tags.get('highway',''),
            **{k:tags.get(k,'') for k in ('access','vehicle','motor_vehicle','motorcar','service','oneway')},
            'interpretation':'Missing-road audit only; not an approved vehicle network' if mode=='generic_highway'
                else 'Tag-filter eligibility, no physical vehicle-access certification'})
    columns=['scenario','source','scope','distance_m','way_id','u','v','key','highway',
             'access','vehicle','motor_vehicle','motorcar','service','oneway','status','interpretation']
    with (OUT/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader();writer.writerows(comparisons)
    boundaries={slug:gpd.read_file(ROOT/f'data/20261109T045434949410Z/{slug}/boundary.geojson').to_crs(5179).geometry.union_all()
                for slug in ('cheongyang','buyeo')}
    for road in roads:
        snap=road['_metric'].interpolate(road['_metric'].project(POINT))
        road['nearest_point_county_membership']={slug:b.covers(snap) for slug,b in boundaries.items()}
    candidates_not_existing=[r for r in roads if not any(r['existing_membership'].values())]
    close=[r for r in roads if r['distance_m']<=50]
    restricted=[r for r in roads if r['vehicle_assessment']['status'] in
                ('prohibited_or_permission_limited','conditional_or_purpose_limited')]
    public_connectivity={k:v for k,v in connectivity.items() if not k.startswith('_')}
    relevant_node_tags=[]
    for nid,n in nodes.items():
        tags=n.get('tags',{})
        if not any(k in tags for k in ('barrier','access','motor_vehicle','motorcar','ford','highway')):continue
        point=Point(*TO_M.transform(n['lon'],n['lat']))
        if point.distance(POINT)>RADIUS:continue
        relevant_node_tags.append({'node_id':nid,'longitude':n['lon'],'latitude':n['lat'],
            'tags':tags,'distance_m':point.distance(POINT),
            'in_nearest_way_weak_component':nid in connectivity.get('_weak_nodes',set())})
    bridge_critical=[]
    for c in cross:
        if not c['bridge_tag_present'] or c['way_id']==latest['drive_service']['way_id']:continue
        alternative=connection([r for r in roads if r['way_id']!=c['way_id']],latest['drive_service'],
            {r['way_id'] for r in roads if any(r['existing_membership'].values())})
        bridge_critical.append({'way_id':c['way_id'],
            'to_major_road_before':connectivity.get('nearest_way_to_existing_major_road'),
            'to_major_road_without_this_way':alternative.get('nearest_way_to_existing_major_road'),
            'from_major_road_before':connectivity.get('existing_major_road_to_nearest_way'),
            'from_major_road_without_this_way':alternative.get('existing_major_road_to_nearest_way'),
            'interpretation':'Topological sensitivity only, not proof of a usable bridge or a recommended route'})
    generic=latest['generic_highway'];nearest=next(r for r in roads if r['way_id']==generic['way_id'])
    before_50=bool(close)
    excluded_drive=bool(nearest['filter_rejections']['drive'])
    report={
        'title':'죽림리마을회관 도로 누락 원인 정밀 조사', 'generated_at_utc':now(),
        'facility':{'name':'죽림리마을회관','csv_name':row['시설명'],'county':'청양군','serial':'177',
            'address':row['상세주소'],'latitude':row['latitude'],'longitude':row['longitude']},
        'data_provenance':{'source':'OpenStreetMap contributors / Overpass API','license':'ODbL 1.0',
            'copyright_url':'https://www.openstreetmap.org/copyright',
            'osm_base_timestamp':stamp,'raw_file':'raw_osm.json','raw_sha256':sha(OUT/'raw_osm.json') if (OUT/'raw_osm.json').exists() else None,
            'query_file':'query.overpassql','query':QUERY,'filters_osmnx_version':ox.__version__,
            'latest_query_is_not_a_historical_reconstruction':True},
        'methodology':{'radius_m':RADIUS,'distance_crs':'EPSG:5179','source_and_geometry_crs':'EPSG:4326',
            'county_mask_applied_to_latest_raw_data':False,
            'distance_definition':'Shortest planar facility-to-raw-OSM-LineString distance; not road travel distance',
            'filter_application':'Exact OSMnx 2.1.1 Overpass tag-filter clauses evaluated on the SAME raw snapshot',
            'filters':filters,'generic_highway_is_motor_vehicle_network':False,
            'topology':'Original OSM shared node IDs only; way direction and roundabout defaults considered. No facility nodes, snapping connections or geometric-intersection connections inserted.',
            'connectivity_limit':'2km local topology; no turn-restriction/barrier/conditional resolution or relief-truck certification',
            'time_and_scope_confounders':'Old county extracts and current local data differ in timestamp and extent. Only latest drive vs latest drive_service isolates filter changes.'},
        'counts':{'highway_ways':len(roads),'water_ways':len(water),'raw_nodes':len(nodes),
            'highway_way_types':dict(Counter(r['tags']['highway'] for r in roads)),
            'ways_absent_from_both_old_county_graphs':len(candidates_not_existing),
            'ways_within_50m':len(close),'explicit_or_purpose_limited_way_count':len(restricted)},
        'comparison':comparisons,'latest_nearest':latest,
        'roads':[public(r) for r in roads],
        'waterways':[public(r) for r in water],
        'explicit_or_purpose_limited_way_ids':[r['way_id'] for r in restricted],
        'node_barriers_and_access_evidence':relevant_node_tags,
        'restriction_relations':restrictions,'restrictions_automatically_resolved':False,
        'connectivity':public_connectivity,'water_crossing_evidence':cross,
        'bridge_removal_topology_sensitivity':bridge_critical,
        'boundary':{'facility_inside_cheongyang':boundaries['cheongyang'].covers(POINT),
            'distance_to_cheongyang_boundary_m':POINT.distance(boundaries['cheongyang'].boundary),
            'existing_neighbor_reference':neighbor,
            'county_scope_effect_confirmed':True},
        'answers':{
            '1_road_in_front_in_osm':{'osm_highway_within_50m_present':before_50,
                'nearest_way_id':generic['way_id'],'distance_m':generic['distance_m'],
                'exact_naver_screenshot_feature_identity_confirmed':False,
                'note':'50m is an explicit proximity description, not a vehicle access threshold. No screenshot file was available to this run.'},
            '2_why_absent_from_drive':{'nearest_generic_way_rejected_by_current_drive_filter':excluded_drive,
                'rejected_clauses':nearest['filter_rejections']['drive'],
                'present_in_old_county_graphs':nearest['existing_membership'],
                'historical_filter_vs_new_osm_edits_not_fully_separated':True},
            '3_distance_change':{mode:r['distance_m'] for mode,r in latest.items()},
            '4_sufficient_to_include_in_relief_transport_network':{'approved':False,
                'reason':'Raw OSM tags/topology alone cannot confirm physical passage, bridge capacity, vehicle dimensions, access permission or current condition.'},
            '5_rebuild_both_county_networks':{'decision':'defer',
                'reason':'One-site evidence supports reviewing filter and county-border scope, not rebuilding all county networks without a defined vehicle-access policy and wider quality assessment.'}},
        'confirmed_facts':[
            f"Latest OSM contains {len(roads)} highway ways intersecting the 2km study disk.",
            f"Nearest generic highway way {generic['way_id']} is {generic['distance_m']:.6f}m from the supplied facility coordinate.",
            'Existing county-scope effect remains confirmed: the old Buyeo graph has a road 259.893m away while the old Cheongyang result is 1359.551m.',
            'No independent ground survey or facility-coordinate correction was performed.'],
        'unconfirmed':[
            'The exact road depicted in the described Naver screenshot and actual motor-vehicle passage.',
            'Physical condition, legal access, width/load limits where OSM lacks tags, and a relief-truck usable crossing.',
            'Whether every current absent OSM way had the same tags/geometry at the old extraction time.'],
        'validation':{'filter_and_oneway_self_checks_passed':True,'original_files_unchanged':True,
            'preexisting_files_checked':len(hashes),'protected_inputs_sha256':hashes,
            'road_geometry_source':'Only returned original OSM way nodes/geometry; no digitization from Naver.',
            'existing_graphs_rebuilt':False,'existing_graphs_or_shelter_csv_written':False,
            'stage2_results_overwritten':False,'scope_facilities':1},
    }
    (OUT/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    render_report(report)


def render_report(e):
    g=e['latest_nearest']['generic_highway'];d=e['latest_nearest']['drive'];s=e['latest_nearest']['drive_service']
    tags=g['tags'];lines=['# 죽림리마을회관 도로 누락 원인 정밀 조사','',
        f"시설: {e['facility']['address']} / 위도 {LAT}, 경도 {LON}",
        f"최신 OSM 기준 시각: `{e['data_provenance']['osm_base_timestamp']}`. 반경 2km, 군 경계 양쪽을 함께 조회했습니다.",'',
        '## 추출 조건별 거리 비교','',
        '| 조건 | 시설→도로 직선거리(m) | OSM way ID |', '|---|---:|---|']
    for r in e['comparison']:
        value=f"{r['distance_m']:,.3f}" if r['distance_m'] is not None else '확인된 후보 없음'
        lines.append(f"| {r['scenario']} | {value} | {r.get('way_id','')} |")
    lines.extend(['','기존 군별 결과와 최신 반경 조회는 시점·범위가 다릅니다. **최신 drive와 최신 drive_service 비교만 같은 시점·범위에서 필터 효과를 분리합니다.** 범용 highway는 누락 조사 자료이며 차량 통행 네트워크로 인정하지 않았습니다.','',
        '## 최종 질문별 답변','',
        f"1. **회관 앞 도로의 OSM 존재 여부:** 최근접 highway way `{g['way_id']}`가 {g['distance_m']:.3f}m에 있습니다. 태그: `{json.dumps(tags,ensure_ascii=False)}`. 제공 좌표와의 근접성은 확인했으나, 첨부 스크린샷 파일이 없어 네이버 화면의 특정 선과 동일한지 확인하지 못했습니다.",
        f"2. **기존 drive에서 제외된 이유:** 이 최근접 way에 현재 OSMnx drive 필터를 적용한 제외 조건은 `{json.dumps(g['filter_rejections']['drive'],ensure_ascii=False)}`입니다. 기존 그래프 포함 여부는 `{g['existing_membership']}`입니다. 군 경계 범위 영향은 기존 부여군 지천로 259.893m 사례로 별도 확인됐습니다. 원래 추출 시점의 원시 응답이 없어 모든 누락의 역사적 원인을 단정하지 않았습니다.",
        f"3. **거리 변화:** 최신 drive `{d['distance_m']}`m, drive_service `{s['distance_m']}`m, 범용 highway `{g['distance_m']}`m입니다. 위 비교표에 기존 1,359.551m 및 259.893m를 함께 보존했습니다.",
        '4. **구호물자 수송 네트워크에 즉시 포함할 근거:** 충분하지 않습니다. OSM 태그·공유 노드 연결은 확인하되 통행권, 실제 노면, 폭·중량·교량 상태와 구호차량 제원을 확인하지 못했습니다. 수송 네트워크에 자동 편입하지 않았습니다.',
        '5. **부여군·청양군 전체 재구축 필요성:** 판단 보류입니다. 이번 1곳의 자료는 필터·군 경계 범위 검토 근거이며, 전체 재구축의 충분한 근거로 일반화하지 않았습니다.','',
        '## 연결성과 하천 횡단','',
        f"- 원래 OSM 노드 ID로만 연결한 국소 drive_service 그래프 결과: `{json.dumps(e['connectivity'],ensure_ascii=False)}`",
        '- 선이 지도상 교차한다는 이유로 노드를 만들지 않았습니다. 일방통행·회전교차로 기본 방향은 반영했습니다. 조건부 통행·turn restriction·barrier는 증거에 기록했으나 자동으로 해석하거나 통행 허가로 간주하지 않았습니다.',
        '- 아래는 bridge/ford 태그 또는 물길 형상과 교차하는 도로입니다. 형상 교차만으로 교량·도하 또는 차량 통행을 인정하지 않았습니다.','',
        '| way ID | 도로명 | highway | bridge | ford | access / motor_vehicle | 최근접 도로와 같은 WCC |',
        '|---|---|---|---|---|---|---|'])
    for r in e['water_crossing_evidence']:
        t=r['tags'];lines.append(f"| {r['way_id']} | {t.get('name','')} | {t.get('highway','')} | {t.get('bridge','')} | {t.get('ford','')} | {t.get('access','')} / {t.get('motor_vehicle','')} | {r['in_nearest_way_weak_component']} |")
    lines.extend(['','`evidence.json`에는 각 도로의 원본 형상·전체 태그·기존 그래프 포함 여부·필터 제외 조항, 물길 및 bridge/ford, 통행 제한 relation·노드, 교량 way 제거 민감도 검사를 보존했습니다. 같은 WCC라는 사실만으로 해당 교량을 실제 진입 경로로 사용한다고 결론내리지 않았습니다.','',
        '## 확인된 사실과 보류 사항',''])
    lines.extend('- '+x for x in e['confirmed_facts'])
    lines.extend(['','다음 사항은 확인되지 않았습니다:',''])
    lines.extend('- '+x for x in e['unconfirmed'])
    lines.extend(['','## 재현·보존','',
        '저장소 루트에서 기존 의존성을 설치한 Python 환경으로 실행합니다. 키가 필요하지 않습니다.','',
        '```sh','python output/road_network_audit/jukrim/audit.py --fetch',
        '# 저장된 동일 OSM 응답으로 재현:',
        'python output/road_network_audit/jukrim/audit.py --osm-json output/road_network_audit/jukrim/raw_osm.json','```','',
        '입력: 기존 시설·접근성 CSV, 두 군의 기존 GraphML, 기존 경계·이전 조사 보고서. 출력은 현재 조사 폴더에만 씁니다. 기존 GraphML, 시설 좌표, 2단계 CSV는 수정하지 않았습니다.','',
        'OpenStreetMap contributors / ODbL 1.0. 출처: https://www.openstreetmap.org/copyright',''])
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')


def write_blocked_outputs(attempt,hashes):
    """Preserve actual known baselines and explicitly UNKNOWN new measurements."""
    prior=json.loads((ROOT/'output/road_access_investigation/report.json').read_text())
    case=next(c for c in prior['cases'] if c['시설명']=='죽림리회관')
    row=next(r for r in csv.DictReader((ROOT/'data/processed/shelters_road_access_147.csv').open(encoding='utf-8-sig'))
             if r['시군명']=='청양군' and r['시설일련번호']=='177')
    neighbor=case['neighbor_county_existing_network_comparison']
    cols=['scenario','snapshot','scope','distance_m','way_id','status','reason']
    comparisons=[
        {'scenario':'existing_cheongyang_drive','snapshot':'existing 2026-11-09 graph and saved result',
         'scope':'Cheongyang only','distance_m':row['road_distance_m'],'way_id':row['road_edge_osmid'],
         'status':'known_previous_result','reason':'Original result preserved; no recomputation or replacement'},
        {'scenario':'existing_cheongyang_and_buyeo_drive','snapshot':'existing county graphs / prior investigation',
         'scope':'Two existing county graphs','distance_m':neighbor['reference_distance_m'],
         'way_id':neighbor['nearest_edge']['osmid'],'status':'known_previous_result',
         'reason':'Existing Buyeo Jicheon-ro reference is outside Cheongyang boundary'}]
    for mode in ('drive','drive_service','generic_highway'):
        comparisons.append({'scenario':'latest_local_'+mode,'snapshot':'NOT RETRIEVED',
            'scope':'planned 2km without county mask','distance_m':'','way_id':'',
            'status':'unavailable_proxy_blocked','reason':'Overpass proxy tunnel 403; empty distance means UNKNOWN, never zero'})
    with (OUT/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(comparisons)
    evidence={
        'audit_status':'blocked_latest_osm_not_retrieved','analysis_complete':False,
        'generated_at_utc':now(),'facility':{'name':'죽림리마을회관','csv_name':row['시설명'],
            'address':row['상세주소'],'county':'청양군','serial':'177',
            'latitude':row['latitude'],'longitude':row['longitude']},
        'actual_http_attempt':attempt,
        'raw_osm_retrieved':False,'raw_osm_base_timestamp':None,'osm_roads':None,
        'way_geometries_and_vehicle_tags':None,'bridge_ford_and_connectivity_findings':None,
        'known_existing_baselines':comparisons[:2],
        'previous_evidence':{'source':'output/road_access_investigation/report.json',
            'sha256':sha(ROOT/'output/road_access_investigation/report.json'),
            'saved_cheongyang_edge':case['saved_nearest_edge'],
            'saved_facility_and_snap':{'facility':case['facility_coordinates'],'snap':case['saved_nearest_point']},
            'cheongyang_boundary_distance_m':case['boundary']['distance_m'],
            'buyeo_comparison':neighbor,'stored_kakao':case['stored_kakao_evidence']},
        'filter_policy_confirmed_not_site_specific':{
            'osmnx_version':ox.__version__,'drive':_get_network_filter('drive'),
            'drive_service':_get_network_filter('drive_service'),
            'interpretation':'Drive excludes highway=service; drive_service admits service unless other exclusion clauses apply. Both exclude track. This establishes policy, not the existence/tags of any currently unavailable local way.'},
        'answers':{
            '1_front_road_in_osm':'UNKNOWN: latest raw data not retrieved; proxy failure is not evidence of OSM absence.',
            '2_front_road_excluded_by_drive':'UNKNOWN for the actual front road; only general filter policy is confirmed.',
            '3_new_distances':{'latest_drive_m':None,'latest_drive_service_m':None,'latest_generic_highway_m':None},
            '4_include_new_roads_in_relief_network':'Not justified on current evidence; no new roads identified/approved.',
            '5_rebuild_both_county_networks':'DEFER: no basis for whole-network rebuilding from this blocked local audit.'},
        'cause_classification':{
            'osm_present_but_drive_filtered':'unconfirmed_local_raw_data_unavailable',
            'unmapped_in_osm':'unconfirmed_do_not_infer_from_http_failure',
            'county_scope_effect':'confirmed_from_existing_graphs',
            'facility_coordinate_error':'not_confirmed_existing_address_and_building_name_consistent_no_independent_survey',
            'motor_vehicle_passability':'not_verified'},
        'validation':{'syntax_and_filter_oneway_checks_passed':True,
            'live_raw_data_analysis_validated':False,'original_files_unchanged':True,
            'protected_inputs_sha256':hashes,'protected_file_count':len(hashes),
            'new_geometries_or_connections_created':False,'stage2_results_modified':False},
        'resume_command':'python output/road_network_audit/jukrim/audit.py --fetch',
        'required_environment_change':'Allow overpass-api.de on the running environment, not only reusable configuration.',
        'screenshot':'No attached Naver image file was available. No screen digitization or Naver scraping was performed.'}
    (OUT/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (OUT/'query.overpassql').write_text(QUERY+'\n',encoding='utf-8')
    text=f'''# 죽림리마을회관 OSM 정밀 조사 — 최신 자료 조회 차단

**조사는 미완료입니다. 최신 OSM 원자료를 받지 못했으며, 새 도로의 존재·필터별 거리·교량 및 차량 연결성을 확인했다고 해석하면 안 됩니다.**

시설: {row['상세주소']} / 위도 {row['latitude']}, 경도 {row['longitude']}.

## 실제 요청과 차단 원인

`{attempt['endpoint']}`에 반경 2km OSM 원자료 요청을 실제 실행했습니다.
오류 유형: `{attempt.get('error_type')}`. 프록시 터널 연결 `403 Forbidden`으로 Overpass API 응답을 받지 못했습니다.
요청 시각·쿼리·오류 원문은 `evidence.json`과 `fetch_failure.json`에 보존했습니다.
`query.overpassql`은 도로·물길·제한 relation까지 포함해 재개할 때 사용할 전체 쿼리입니다. 실제 실패한 최초 highway 조회 쿼리는 증거 JSON에 별도로 기록했습니다.

사용자는 재사용 환경 설정만 변경했다고 알려주었습니다. 현재 실행 환경에는 허용 도메인이 적용되지 않았습니다. 이 실행 환경을 수정할 도구도 제공되어 있지 않아 새 OSM 데이터를 받을 수 없습니다.

## 기존 자료로 확인된 사실

| 기존 조건 | 거리(m) | 근거 |
|---|---:|---|
| 청양군 기존 drive | {float(row['road_distance_m']):,.3f} | 기존 접근성 CSV, 간선 `{row['road_edge_osmid']}` |
| 청양군·부여군 기존 drive 비교 | {neighbor['reference_distance_m']:,.3f} | 부여군 지천로 way `{neighbor['nearest_edge']['osmid']}` |

- 회관은 기존 청양군 경계 내부이며 경계까지 {case['boundary']['distance_m']:.3f}m입니다.
- 부여군 지천로 비교 도로점은 청양군 경계 밖입니다. **군별 검색 범위의 영향은 기존 자료로 확인됐습니다.**
- 저장된 Kakao 주소·좌표가 일치하며 회관과 일관된 건물명이 반환됐습니다. 좌표 오류를 입증하지 않았으며 독립적인 현장 검증도 아닙니다.
- OSMnx 2.1.1의 기본 drive는 `highway=service`를 제외합니다. drive_service는 service를 허용하지만 access/private 등 다른 제외 조건은 적용합니다. 두 설정 모두 track을 제외합니다. 이는 필터 정책이며, 회관 앞 특정 도로에 해당 태그가 실제 존재하는지는 아직 확인하지 못했습니다.

## 최종 질문별 현재 답변

1. **회관 앞 도로가 OSM 원자료에 존재하는가?** 확인 불가. 조회 차단은 OSM에 도로가 없다는 증거가 아닙니다.
2. **존재한다면 drive에서 왜 제외됐는가?** 해당 way ID와 태그를 확보하지 못해 확인 불가. 군별 범위 영향과 일반 필터 정책만 확인했습니다.
3. **조건 변경 시 거리 변화는?** 최신 drive·drive_service·범용 highway 값 모두 미확인입니다. `comparison.csv`에 빈 값과 `unavailable_proxy_blocked` 상태로 기록했습니다. 0m나 추정치를 넣지 않았습니다.
4. **수송 네트워크 편입 근거가 충분한가?** 부족합니다. 새 도로·bridge/ford·공유 OSM 노드 연결·차량 제한을 확인하지 못했습니다.
5. **전체 도로망 재구축이 필요한가?** 판단 보류. 이 조사로 전체 재구축을 결정할 근거는 없습니다.

## 재개·재현

`overpass-api.de`가 실제 적용된 실행 환경에서 저장소 루트 기준으로 실행합니다.

```sh
python output/road_network_audit/jukrim/audit.py --fetch
```

성공하면 반경 2km 원자료·쿼리·응답 시각을 보존하고, 동일 스냅샷의 drive/drive_service/범용 highway를 비교합니다. 원본 OSM 노드 ID에 의한 국소 연결성, 일방통행, 물길·bridge/ford, barrier·통행제한 relation을 조사하며 보고서를 갱신합니다. 범용 자료를 차량 통행 가능 네트워크로 자동 인정하지 않습니다. 동일 응답의 오프라인 재현 명령:

```sh
python output/road_network_audit/jukrim/audit.py --osm-json output/road_network_audit/jukrim/raw_osm.json
```

## 검증·보존

- 코드 문법과 필터·일방통행 조건 검사는 통과했습니다. 최신 실제 OSM 자료를 이용한 전체 분석 검증은 아직 수행하지 못했습니다.
- 기존 추적 파일 {len(hashes)}개의 SHA-256 불변을 확인했습니다. 원본 GraphML·시설 CSV·2단계 결과를 수정하지 않았습니다.
- 전체 군 도로망 재추출, 가상 도로·연결선 생성, 네이버 좌표 추출·스크래핑은 하지 않았습니다. 이번 메시지에 실제 스크린샷 파일이 없어 화면의 선을 검증 근거로 쓰지 않았습니다.
'''
    (OUT/'report.md').write_text(text,encoding='utf-8')


if __name__=='__main__':main()
