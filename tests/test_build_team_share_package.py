import base64
from collections import Counter
import csv
from functools import partial
import gzip
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
from threading import Thread
from types import SimpleNamespace
from zipfile import ZipFile

import geopandas as gpd
from pyproj import Transformer
import pytest
from shapely.geometry import LineString, Point, box

from scripts.build_flood_road_scenarios import write_csv
from scripts.build_team_share_package import RIGHTS_ITEMS, build
from scripts.facility_access_scenarios import Span
from scripts.finalize_facility_access_points import scenario_rows
from scripts.flood_road_inputs import FACILITIES, PROFILES, SNAPSHOT, file_sha256
from scripts.team_share_data import FLOOD_STATES, PROJECTION_FILES
from scripts.team_share_geometry import map_coords, physical_pairs, road_shapes

PROFILE = 'relief_vehicle_candidate'
X, Y = 200000, 450000
TO_WGS = Transformer.from_crs(5186, 4326, always_xy=True)


def dump(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')


def write_layer(path, name, rows):
    gpd.GeoDataFrame(rows, crs=5186).to_file(path, layer=name)


def parents_and_traces():
    parents = []
    nodes = []
    for way, y, road_type, tags in (
        (101, Y, 'residential', '{}'),
        (102, Y+255, 'secondary', '{"bridge":"yes","name":"시험 간선로"}'),
    ):
        u, v = way*2, way*2+1
        coords = [(X,y),(X+1000,y)]
        nodes.extend(dict(node_id=n, geometry=Point(p)) for n,p in zip((u,v), coords, strict=True))
        for a, b, reverse in ((u,v,False),(v,u,True)):
            uid = f'edge-{a}-{b}'
            parents.append(dict(
                edge_uid=uid, parent_edge_id=uid, source_segment_ids=uid, source_node_ids=f'{a},{b}',
                osmid=way, oneway=False, reversed=reverse, highway=road_type, osm_tags=tags,
                original_u=a, original_v=b, original_key=0, child_id=f'{uid}:0', u=a, v=b,
                start_m=0.0, end_m=1000.0, length_m=1000.0, state='open',
                geometry=LineString(coords[::-1] if reverse else coords),
            ))
    traces = {}
    for scenario, end in [('S1',400),('S2',700)]:
        records = []
        for i, edge in enumerate(parents):
            uid, a, b = edge['edge_uid'], edge['u'], edge['v']
            start, stop = (1000-end,800) if edge['reversed'] else (200,end)
            cut1, cut2 = -2*i-1,-2*i-2
            for j, (m,n,u,v,state) in enumerate((
                (0,start,a,cut1,'open'),
                (start,stop,cut1,cut2,'blocked' if edge['osmid']==101 else 'review'),
                (stop,1000,cut2,b,'open'),
            )):
                row = {k:v for k,v in edge.items() if k!='geometry'}
                row.update(child_id=f'{uid}:{j}',u=u,v=v,start_m=m,end_m=n,length_m=n-m,state=state)
                records.append(row)
        traces[scenario] = records
    return parents,nodes,traces


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    args = SimpleNamespace(run=tmp_path/'run', road_artifact=tmp_path/'road',
                           facilities=tmp_path/'facility.csv', flood_gpkg=tmp_path/'flood.gpkg',
                           independent_qa=tmp_path/'road_qa.json', projection_dir=tmp_path/'prior',
                           final_run=None, manual_review=None, rights=None, output=tmp_path/'publish')
    final = tmp_path/'final-facility-access-20261124'
    for path in (args.run,args.road_artifact,args.projection_dir,final):
        path.mkdir()
    monkeypatch.setitem(PROFILES, PROFILE, (4,4))
    parents, nodes, traces = parents_and_traces()
    s0 = args.run/'road_scenarios.gpkg'
    write_layer(s0,f'{PROFILE}_s0_edges',parents)
    write_layer(s0,f'{PROFILE}_s0_nodes',nodes)
    road = args.road_artifact/'road_network_profiles.gpkg'
    write_layer(road,f'{PROFILE}_simplified_edges',[dict(row,raw_segments='[0]') for row in parents])
    for s,end in [('S1',400),('S2',700)]:
        write_layer(args.flood_gpkg,f'scenario_{s.lower()}_closure_zone',
                    [dict(geometry=box(X+200,Y-2,X+end,Y+260))])
        write_csv(args.run/f'{PROFILE}_{s.lower()}_edge_trace.csv',traces[s])
    tags = args.road_artifact/'source_node_tags.json'
    dump(tags,{str(nodes[0]['node_id']): {'name:ko': '시험 마을', 'place': 'village'}})
    road_metadata = args.road_artifact/'metadata.json'

    by_trace = {s:{p['edge_uid']:[Span.from_row(r) for r in items if r['edge_uid']==p['edge_uid']]
                   for p in parents} for s,items in traces.items()}
    sources,points,states,links,floods = [],[],[],[],[]
    a,b = parents[:2]
    for n in range(1,147):
        county = '부여군' if n <= 73 else '청양군'
        m = 300 if n <= 16 else 500 if n <= 18 else 800+n/2
        p_lon,p_lat = TO_WGS.transform(X+m,Y)
        f_lon,f_lat = TO_WGS.transform(X+m,Y-10)
        site = dict(access_point_id=f'access:{county}:{n}',county=county,facility_id=str(n),
                    facility_name=f'검증 시설 {n}',address=f'{county} 시험로 {n}',
                    facility_latitude=f_lat,facility_longitude=f_lon,
                    final_projection_latitude=p_lat,final_projection_longitude=p_lon,
                    final_projection_x_5186=X+m,final_projection_y_5186=Y,
                    s0_edge_uid=a['edge_uid'],s0_edge_measure_m=m,final_connector_length_m=10,
                    selection_method='manual_keep' if n <= 26 else 'manual_entrance_refinement' if n <= 30 else 'automatic_projection',
                    review_decision='keep' if n <= 26 else 'revise' if n <= 30 else '',
                    review_reason='지도 확인' if n<=30 else '',review_notes='',
                    highway=a['highway'],s0_direction_count=2,vehicle_entrance_status='unverified')
        points.append(site)
        sources.append({'시군명':county,'시설일련번호':str(n),'시설명':site['facility_name'],
                        '상세주소':site['address'],'latitude':f_lat,'longitude':f_lon})
        status, approaches = scenario_rows(site,[(a,m),(b,1000-m)],by_trace)
        states.extend(status)
        links.extend(approaches)
        for r in status[1:]:
            wet = r['point_state']=='blocked'
            floods.append(dict(
                **{k:r[k] for k in ('access_point_id','county','facility_id','facility_name','scenario')},
                facility_point_mask_state='outside',access_point_mask_state='inside' if wet else 'outside',
                straight_connector_mask_state='partially_overlapped' if wet else 'outside',
                straight_connector_interior_overlap_m=2 if wet else 0,
            ))
    write_csv(args.facilities,sources)
    dump(road_metadata,dict(
        status='data_validated',
        source=dict(data_timestamp='2026-11-20T00:00:00Z',sha256=SNAPSHOT,
                    license='OpenStreetMap contributors / ODbL 1.0'),
        verification=dict(source_directed_segments_preserved=True,graphml_gpkg_roundtrips=True),
        input_sha256={FACILITIES.as_posix():file_sha256(args.facilities)},
        output_sha256={road.name:file_sha256(road),tags.name:file_sha256(tags)},
    ))
    comparison = [dict(profile=p,scenario=s,active_directed_edges=4 if s=='S0' else 10,
                       blocked_directed_child_edges=0 if s=='S0' else 2,
                       physical_blocked_m=n,largest_scc_original_nodes=2)
                  for p in PROFILES for s,n in [('S0',0),('S1',200),('S2',500)]]
    write_csv(args.run/'scenario_comparison.csv',comparison)
    validation = args.run/'validation.json'
    dump(validation,dict(status='passed',analysis_crs='EPSG:5186',facility_sha256=file_sha256(args.facilities),
         flood=dict(sha256=file_sha256(args.flood_gpkg),areas_km2=dict(S1=.0524,S2=.131)),
         roads=dict(sha256=file_sha256(road),metadata_sha256=file_sha256(road_metadata),
                    source=json.loads(road_metadata.read_bytes())['source']),
         output_sha256={r.name:file_sha256(r) for r in args.run.iterdir()},scenarios=comparison))
    dump(args.independent_qa,dict(status='passed',run_gpkg_sha256=file_sha256(s0),
         facility_sha256=file_sha256(args.facilities),
         profiles={PROFILE:dict(scenarios={r['scenario']:r for r in comparison if r['profile']==PROFILE})}))
    prior = {k:args.projection_dir/n for k,n in PROJECTION_FILES.items()}
    dump(prior['projection_summary'],dict(facilities=146,
         inputs={'shelters_final_147.csv':file_sha256(args.facilities),
                 'source_node_tags.json':file_sha256(tags),'s0_road_network.gpkg':'export-digest'},
         projection_distance_m=dict(mean=10,median=10),nearest_eligible_node_m=dict(median=50),
         upper_fence_m=30,extreme_fence_m=70))
    write_csv(prior['automatic'],[dict(road_projection_distance_m=10) for _ in range(146)])
    for kind in ('review_queue','audit_sample'):
        write_csv(prior[kind],[dict(facility_id='1')])
    for name,rows in [('facility_final_access_points.csv',points),('access_point_scenarios.csv',states),
                      ('facility_connector_flood.csv',floods),('access_point_od_connectors.csv',links)]:
        write_csv(final/name,rows)
    manifest = dict(status='passed',profile=PROFILE,analysis_crs='EPSG:5186',facilities=146,
        s0_provenance='original_review_s0_export',selection_methods=Counter(r['selection_method'] for r in points),
        access_point_states={s:Counter(r['point_state'] for r in states if r['scenario']==s) for s in ('S0','S1','S2')},
        wet_mask_states={s:{f:Counter(r[f] for r in floods if r['scenario']==s) for f in FLOOD_STATES} for s in ('S1','S2')},
        modeled_arrival_link_counts={s:{'review_'+p:sum(x['arrival_count_review_'+p] for x in states if x['scenario']==s)
                                        for p in ('closed','open')} for s in ('S0','S1','S2')},
        trace_direction_child_counts={s:Counter(r['state'] for r in t) for s,t in traces.items()},
        output_sha256={f.name:file_sha256(f) for f in final.iterdir()},
        input_sha256={k:dict(sha256=file_sha256(p)) for k,p in dict(
            validation=validation,flood=args.flood_gpkg,facilities=args.facilities,node_tags=tags,
            **prior,**{s:args.run/f'{PROFILE}_{s.lower()}_edge_trace.csv' for s in ('S1','S2')}).items()})
    manifest['input_sha256']['s0']={'sha256':'export-digest'}
    dump(final/'facility_access_qa.json',manifest)
    wrong = tmp_path/'final-facility-access-20261125'
    shutil.copytree(final,wrong)
    altered = dict(manifest,input_sha256=dict(manifest['input_sha256'],S2={'sha256':'wrong'}))
    dump(wrong/'facility_access_qa.json',altered)
    missing = tmp_path/'final-facility-access-20261126'
    missing.mkdir()
    dump(missing/'facility_access_qa.json',manifest)
    failed = tmp_path/'final-facility-access-20261127'
    shutil.copytree(final,failed)
    dump(failed/'facility_access_qa.json',dict(manifest,status='failed'))
    return args,final


def test_partial_shape_and_identity_safe_dedup():
    parents,_,trace = parents_and_traces()
    data = {r['edge_uid']:r for r in parents}
    a, b = parents[:2]
    different_way = dict(a,edge_uid='another-way',osmid=222)
    different_ordinal = dict(a,edge_uid='parallel-occurrence')
    data.update({r['edge_uid']:r for r in (different_way,different_ordinal)})
    indices = {r:(1,) if r == 'parallel-occurrence' else (0,) for r in data}
    pairs = physical_pairs(data,indices)
    assert pairs[0]==(a['edge_uid'],b['edge_uid'])
    assert ('another-way',None) in pairs and ('parallel-occurrence',None) in pairs
    intervals = {uid:[Span.from_row(r) for r in trace['S1'] if r['edge_uid']==uid] for uid in data}
    for row in (different_way,different_ordinal):
        intervals[row['edge_uid']] = [Span.from_row(row)]
    shapes, removed = road_shapes(data,pairs,intervals,{'residential':0,'secondary':1})
    assert removed==6
    assert [item for item in shapes if item[0]==2]==[[2,0,map_coords(LineString([(X+200,Y),(X+400,Y)]))]]
    assert len([item for item in shapes if item[0]==1])==1
    assert len(shapes)==8


def html_data(blob, name):
    token = f'id="{name}">'.encode()
    return blob.split(token,1)[1].split(b'</script>',1)[0]


def test_package_selects_matching_input_and_protects_distribution(inputs):
    args,_ = inputs
    build(args)
    local = (args.output/'START_HERE.html').read_bytes()
    assert len(json.loads(html_data(local,'data'))['sites'])==146
    assert json.loads(gzip.decompress(base64.b64decode(html_data(local,'mask-S2'))))
    offline = json.loads(gzip.decompress(base64.b64decode(html_data(local,'offline-basemap'))))
    assert {(kind,name) for kind,name,_ in offline['labels']} == {
        ('county','부여군'), ('county','청양군'), ('place','시험 마을'), ('road','시험 간선로'),
    }
    assert len(offline['polygons']) >= 2 and len(offline['roads']) == 1
    with ZipFile(args.output/'Kor_map_team_share_light.zip') as light, ZipFile(args.output/'Kor_map_team_share_full.zip') as full:
        assert light.testzip() is None and full.testzip() is None
        assert set(light.namelist()) < set(full.namelist())
        assert all(light.read(n)==full.read(n) for n in light.namelist())
        assert not html_data(light.read('START_HERE.html'),'mask-S2')
        assert html_data(light.read('START_HERE.html'),'offline-basemap') == html_data(local,'offline-basemap')
        assert 'input/road_profile_artifacts/road_network_profiles.gpkg' in full.namelist()
        assert all(not n.endswith(('flood_100y_depth_scenarios.gpkg','road_scenarios.gpkg','edge_trace.csv','shelters_final_147_verified.csv')) for n in full.namelist())
        meta = json.loads(light.read('SHARE_METADATA.json'))
        assert meta['online_basemap']['enabled_on_open']
        assert meta['display']['offline_basemap']['settlement_labels'] == 1
        assert meta['display']['offline_basemap']['county_boundaries']['buyeo']['sha256']
        assert (args.output/'SHARE_METADATA.json').read_bytes() == light.read('SHARE_METADATA.json')
        assert meta['final_run']=='final-facility-access-20261124'
        assert [r.split(':',1)[0] for r in meta['rejected_final_runs']] == [
            'final-facility-access-20261127','final-facility-access-20261126','final-facility-access-20261125']
        manifest = list(csv.DictReader(full.read('manifest_sha256.csv').decode('utf-8-sig').splitlines()))
        assert {r['path'] for r in manifest} == set(full.namelist())-{'manifest_sha256.csv'}
        for r in manifest:
            value = full.read(r['path'])
            assert int(r['bytes']) == len(value)
            assert hashlib.sha256(value).hexdigest() == r['sha256']
    with pytest.raises(ValueError,match='new output'):
        build(args)


def test_verified_redistribution_can_include_originals_and_masks(inputs, tmp_path):
    args,final = inputs
    args.final_run = final
    qa_path = final/'facility_access_qa.json'
    qa=json.loads(qa_path.read_bytes())
    actual_s0 = file_sha256(args.run/'road_scenarios.gpkg')
    qa.update(s0_provenance='archived_full_scenario_gpkg')
    qa['input_sha256']['s0']={'sha256':actual_s0}
    dump(qa_path,qa)
    sha = {k:v['sha256'] for k,v in qa['input_sha256'].items()}
    args.rights = tmp_path/'rights.json'
    dump(args.rights,{k:dict(sha256=sha[i],basis='Synthetic test permission',credit='Fixture')
                     for k,(i,_,_) in RIGHTS_ITEMS.items()})
    build(args)
    with ZipFile(args.output/'Kor_map_team_share_light.zip') as light, ZipFile(args.output/'Kor_map_team_share_full.zip') as full:
        assert light.read('START_HERE.html') == (args.output/'START_HERE.html').read_bytes()
        assert {'input/scenario/road_scenarios.gpkg','input/flood/flood_100y_depth_scenarios.gpkg',
                'input/shelters_final_147_verified.csv'} <= set(full.namelist())
    r = json.loads(args.rights.read_bytes())
    r['flood_display']['sha256']='stale'
    dump(args.rights,r)
    args.output = tmp_path/'rejected'
    with pytest.raises(ValueError,match='matching SHA-256'):
        build(args)
    assert not args.output.exists()


def test_refuses_altered_fixed_coordinate_even_if_output_hash_updated(inputs):
    args,final = inputs
    args.final_run = final
    path=final/'access_point_scenarios.csv'
    with path.open(encoding='utf-8-sig') as f:
        rows=list(csv.DictReader(f))
    rows[1]['final_projection_latitude']='0'
    write_csv(path,rows)
    qa_path=final/'facility_access_qa.json'
    qa=json.loads(qa_path.read_bytes())
    qa['output_sha256'][path.name]=file_sha256(path)
    dump(qa_path,qa)
    with pytest.raises(ValueError,match='fixed position changed'):
        build(args)
    assert not args.output.exists()


def test_lists_missing_analysis_and_manual_review_files_before_writing(inputs):
    args,_ = inputs
    args.manual_review = args.projection_dir/'missing_manual_review.csv'
    projection = args.projection_dir/PROJECTION_FILES['projection_summary']
    projection.unlink()
    with pytest.raises(ValueError,match='Missing required input files') as exc:
        build(args)
    assert str(projection.resolve()) in str(exc.value)
    assert str(args.manual_review.resolve()) in str(exc.value)
    assert not args.output.exists()


@pytest.fixture
def browser_map(inputs):
    chromium = os.getenv('TEAM_SHARE_CHROMIUM')
    if not chromium:
        pytest.skip('Set TEAM_SHARE_CHROMIUM for browser verification')
    playwright = pytest.importorskip('playwright.sync_api')
    args,_ = inputs
    build(args)
    handler = partial(SimpleHTTPRequestHandler,directory=str(args.output))
    server = ThreadingHTTPServer(('127.0.0.1',0),handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with playwright.sync_playwright() as session:
            with session.chromium.launch(executable_path=chromium, args=['--disable-dev-shm-usage']) as browser:
                page = browser.new_page(viewport={'width':1440,'height':940})
                errors = []
                page.on('pageerror',lambda e:errors.append(str(e)))
                yield page, f'http://127.0.0.1:{server.server_port}/START_HERE.html', playwright.expect
                assert not errors
    finally:
        server.shutdown()
        server.server_close()


def test_chromium_interactive_map_with_all_external_requests_blocked(browser_map):
    page, url, expect = browser_map
    external = []
    page.route('https://**/*', lambda route:(external.append(route.request.url),route.abort()))
    page.goto(url,wait_until='domcontentloaded')
    expect(page.locator('#basemap')).to_have_attribute('data-mode','offline')
    expect(page.locator('#offline-legend')).to_be_visible()
    page.wait_for_function('offlineGeometry?.polygons.length >= 2')
    assert external and all('cdn.jsdelivr.net' in u for u in external)
    attempts = len(external)
    # County shapes are visible without external data when the map is over a saved county.
    page.evaluate('map.setView([36.28,126.91], 12)')
    page.wait_for_function("""() => {
        const pixels = ctx.getImageData(0,0,canvas.width,canvas.height).data;
        for (let i=0; i<pixels.length; i+=4) {
            if (pixels[i] === 237 && pixels[i+1] === 241 && pixels[i+2] === 231 && pixels[i+3] === 255) return true;
        }
        return false;
    }""")
    page.locator('#extent').click()
    expect(page.locator('#facility-list button')).to_have_count(146)
    expect(page.locator('#stat-open')).to_have_text('146')
    page.wait_for_function('geometry !== null')
    page.locator('[data-scenario=S1]').click()
    page.wait_for_function("geometry !== null && geometry.paths[2].length === 1 && geometry.polygons.length === 1")
    expect(page.locator('#stat-blocked')).to_have_text('16')
    assert page.evaluate("roadColors") == ['#127349','#b16117','#bd3239']
    # The intersecting viewport must actually contain each road state color.
    page.evaluate("""() => {
        window.canvasContainsRoadColor = (r,g,b) => {
            const pixels = ctx.getImageData(0,0,canvas.width,canvas.height).data;
            for (let i=0; i<pixels.length; i+=4) {
                if (pixels[i+3] > 175 && Math.abs(pixels[i]-r) <= 4
                    && Math.abs(pixels[i+1]-g) <= 4 && Math.abs(pixels[i+2]-b) <= 4) return true;
            }
            return false;
        };
    }""")
    page.wait_for_function('''(canvasContainsRoadColor(18,115,73) || canvasContainsRoadColor(44,123,88))
        && canvasContainsRoadColor(177,97,23) && canvasContainsRoadColor(189,50,57)''')
    page.locator('[data-scenario=S2]').click()
    page.wait_for_function('geometry !== null')
    expect(page.locator('#stat-open')).to_have_text('128')
    expect(page.locator('#stat-blocked')).to_have_text('18')
    page.locator('#facility-state').select_option('blocked')
    expect(page.locator('#facility-list button')).to_have_count(18)
    page.locator('#search').fill('검증 시설 18')
    expect(page.locator('#facility-list button')).to_have_count(1)
    page.evaluate("markers.getLayers()[0].fire('click')")
    expect(page.locator('#details')).to_contain_text('검증 시설 18')
    expect(page.locator('#details')).to_contain_text('미확인 (unverified)')
    expect(page.locator('#details')).to_contain_text('차단')
    page.locator('#layer-facility').uncheck()
    assert page.evaluate('markers.getLayers().length')==1
    page.locator('#layer-access').uncheck()
    page.locator('#layer-connector').uncheck()
    assert page.evaluate('markers.getLayers().length + connectors.getLayers().length')==0
    page.locator('#county').select_option('청양군')
    expect(page.locator('#facility-list button')).to_have_count(0)
    page.locator('#road-type').select_option(label='secondary')
    page.wait_for_function('!canvasContainsRoadColor(189,50,57) && canvasContainsRoadColor(177,97,23)')
    page.locator('#layer-1').uncheck()
    page.wait_for_function('!canvasContainsRoadColor(177,97,23)')
    page.locator('#layer-flood').uncheck()
    zoom = page.evaluate('map.getZoom()')
    page.locator('.leaflet-control-zoom-in').click()
    assert page.evaluate('map.getZoom()') > zoom
    page.locator('#extent').click()
    assert len(external) == attempts


@pytest.fixture
def simulated_map_service(browser_map):
    """Serve deterministic tiles through Playwright while running the real MapLibre release."""
    assets_dir = os.getenv('TEAM_SHARE_MAPLIBRE_ASSETS')
    if not assets_dir:
        pytest.skip('Set TEAM_SHARE_MAPLIBRE_ASSETS for MapLibre browser verification')
    from PIL import Image

    page, url, expect = browser_map
    state = dict(fault=None, tiles=[], style_requests=0, vector=False)
    tile_color = (173, 198, 225)
    responses = {}
    for name, color in [('tile', tile_color), ('error', (248, 0, 151))]:
        buffer = BytesIO()
        Image.new('RGB', (256,256), color).save(buffer, format='PNG')
        responses[name] = buffer.getvalue()
    base = 'https://tiles.openfreemap.org'
    raster = dict(version=8, sources={'tiles':dict(type='raster', tileSize=256, maxzoom=18,
                                                  tiles=[base+'/test/{z}/{x}/{y}.png'])},
                  layers=[dict(id='reference', type='raster', source='tiles')])
    vector = dict(version=8, sources={'tiles':dict(type='vector', maxzoom=18,
                                                  tiles=[base+'/test/{z}/{x}/{y}.pbf'])},
                  layers=[{'id':'reference', 'type':'fill', 'source':'tiles', 'source-layer':'area',
                           'paint':{'fill-color':'#adc6e1'}}])
    # One synthetic MVT polygon fills its tile. Exercise the vector worker request path.
    geometry = bytes.fromhex('09 00 00 1a 80 40 00 00 80 40 ff 3f 00 0f')
    feature = b'\x18\x03\x22' + bytes([len(geometry)]) + geometry
    layer = b'\x78\x02\x0a\x04area\x12' + bytes([len(feature)]) + feature + b'\x28\x80\x20'
    pbf = b'\x1a' + bytes([len(layer)]) + layer
    cors = {'access-control-allow-origin':'*'}
    assets = {
        name: (Path(assets_dir)/name).read_bytes() for name in ('maplibre-gl.js','maplibre-gl.css')
    }

    def handle(route):
        address = route.request.url
        if address.startswith('https://cdn.jsdelivr.net/'):
            name = address.rsplit('/',1)[1]
            kind = 'text/css' if name.endswith('.css') else 'text/javascript'
            route.fulfill(content_type=kind, headers=cors, body=assets[name])
        elif address == base+'/styles/liberty':
            state['style_requests'] += 1
            route.fulfill(content_type='application/json', headers=cors,
                          body=json.dumps(vector if state['vector'] else raster))
        elif address.startswith(base+'/test/'):
            fault = state['fault']
            if fault == 'partial':
                fault = 403 if len(state['tiles']) == 2 else None
            state['tiles'].append(fault)
            if fault == 'abort':
                route.abort('internetdisconnected')
            elif address.endswith('.pbf') and not fault:
                route.fulfill(content_type='application/vnd.mapbox-vector-tile', headers=cors, body=pbf)
            else:
                route.fulfill(status=fault or 200, content_type='image/png', headers=cors,
                              body=responses['error' if fault else 'tile'])
        else:
            route.abort()

    page.route('https://**/*', handle)
    return page, url, expect, state, tile_color


def test_default_maplibre_basemap_renders_below_analysis_and_can_be_hidden(simulated_map_service):
    from PIL import Image

    page, url, expect, state, tile_color = simulated_map_service
    state['vector'] = True
    page.goto(url, wait_until='domcontentloaded')
    expect(page.locator('#basemap')).to_have_attribute('data-mode','online')
    expect(page.locator('#offline-legend')).to_be_hidden()
    expect(page.locator('.leaflet-control-attribution')).to_contain_text('© OpenMapTiles')
    assert state['style_requests'] == 1 and state['tiles']
    screenshot = Image.open(BytesIO(page.locator('#map').screenshot())).convert('RGB')
    palette = screenshot.getcolors(screenshot.width*screenshot.height)
    assert any(color == tile_color and count > 10000 for count,color in palette)
    assert page.evaluate("Number(mapLayer.style.zIndex) > Number(map.getPane('onlineBasemap').style.zIndex)")
    page.locator('[data-scenario=S1]').click()
    page.wait_for_function('geometry?.polygons.length === 1')
    assert page.evaluate('markers.getLayers().length + connectors.getLayers().length') == 438
    expect(page.locator('#stat-blocked')).to_have_text('16')
    initial_zoom = page.evaluate('map.getZoom()')
    page.locator('.leaflet-control-zoom-in').click()
    assert page.evaluate('map.getZoom()') == initial_zoom + 1
    initial_center = page.evaluate('map.getCenter().lng')
    page.evaluate('map.panBy([180,60], {animate:false})')
    assert page.evaluate('map.getCenter().lng') > initial_center
    page.locator('#search').fill('검증 시설 1')
    page.locator('#facility-list button').first.click()
    expect(page.locator('#details')).to_contain_text('검증 시설 1')
    page.locator('#layer-flood').uncheck()
    expect(page.locator('#basemap')).to_have_attribute('data-mode','online')
    page.locator('#basemap').click()
    expect(page.locator('#basemap')).to_have_attribute('data-mode','hidden')
    expect(page.locator('.maplibregl-canvas')).to_have_count(0)
    expect(page.locator('#offline-legend')).to_be_hidden()
    expect(page.locator('#stat-blocked')).to_have_text('16')


@pytest.mark.parametrize('fault,message', [
    (403,'HTTP 403'), (429,'HTTP 429'), ('abort','연결에 실패'), ('partial','HTTP 403'),
])
def test_basemap_tile_errors_stop_requests_and_allow_manual_retry(simulated_map_service, fault, message):
    page, url, expect, state, _ = simulated_map_service
    state['fault'] = fault
    page.goto(url, wait_until='domcontentloaded')
    expect(page.locator('#basemap')).to_have_attribute('data-mode','offline')
    expect(page.locator('#basemap')).to_have_attribute('aria-pressed','false')
    expect(page.locator('#basemap-status')).to_contain_text(message)
    expect(page.locator('.maplibregl-canvas')).to_have_count(0)
    expect(page.locator('#offline-legend')).to_be_visible()
    if fault == 'partial':
        assert state['tiles'][:3] == [None, None, 403]
    requests = len(state['tiles'])
    assert requests > 0
    page.evaluate('map.panBy([200,100], {animate:false})')
    page.locator('[data-scenario=S2]').click()
    page.wait_for_function('geometry?.polygons.length === 1')
    expect(page.locator('#stat-open')).to_have_text('128')
    expect(page.locator('#stat-blocked')).to_have_text('18')
    page.wait_for_timeout(350)
    assert len(state['tiles']) == requests and state['style_requests'] == 1
    state['fault'] = None
    page.locator('#basemap').click()
    expect(page.locator('#basemap')).to_have_attribute('data-mode','online')
    expect(page.locator('#offline-legend')).to_be_hidden()
    expect(page.locator('#stat-blocked')).to_have_text('18')
    assert len(state['tiles']) > requests and state['style_requests'] == 2
