"""Offline one-site PBF audit. Only download mode uses the network (Geofabrik)."""
from __future__ import annotations
import argparse
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://download.geofabrik.de/asia/south-korea-latest.osm.pbf'
UA = 'KorMapAcademicResearch/1.0 (+https://github.com/hajunben-gif/Kor_map)'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def file_hash(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def audit_module():
    spec = importlib.util.spec_from_file_location('jukrim_existing_audit', ROOT/'output/road_network_audit/jukrim/audit.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def download(work):
    import requests
    import osmium
    work.mkdir(parents=True, exist_ok=False)
    pbf = work/'south-korea.osm.pbf'
    metadata = {'source_url': URL, 'license': 'ODbL 1.0 / OpenStreetMap contributors',
                'copyright': 'https://www.openstreetmap.org/copyright',
                'started_at_utc': datetime.now(timezone.utc).isoformat()}
    # No alternative source, automatic retries, or automatic redirects.
    session = requests.Session()
    session.headers['User-Agent'] = UA
    checksum = session.get(URL+'.md5', timeout=(15, 60), allow_redirects=False)
    if checksum.status_code != 200:
        raise RuntimeError(f'Official checksum download failed: HTTP {checksum.status_code}')
    expected = checksum.text.split()[0].lower()
    if len(expected) != 32 or any(c not in '0123456789abcdef' for c in expected):
        raise ValueError('Invalid official MD5 file')
    with session.get(URL, stream=True, timeout=(15, 180), allow_redirects=False) as response:
        if response.status_code != 200:
            raise RuntimeError(f'Official PBF download failed: HTTP {response.status_code}')
        metadata['http_headers'] = {k: response.headers[k] for k in ('Last-Modified', 'ETag', 'Content-Length') if k in response.headers}
        with pbf.open('xb') as target:
            for block in response.iter_content(1024*1024):
                target.write(block)
    if file_hash(pbf, 'md5') != expected:
        raise ValueError('PBF checksum mismatch; stop without using substitute data')
    reader = osmium.io.Reader(str(pbf))
    try:
        header = reader.header()
        timestamp = header.get('osmosis_replication_timestamp')
        if not timestamp:
            raise ValueError('PBF lacks data timestamp; cannot establish snapshot provenance')
        metadata.update(data_timestamp=timestamp, generator=header.get('generator'),
                        replication_sequence=header.get('osmosis_replication_sequence_number'))
    finally:
        reader.close()
    metadata.update(md5=expected, sha256=file_hash(pbf), bytes=pbf.stat().st_size,
                    finished_at_utc=datetime.now(timezone.utc).isoformat())
    write_json(work/'source.json', metadata)


def extract(work):
    """Three streaming passes; nationwide node coordinates live on disk, not RAM."""
    import sqlite3
    import osmium
    from shapely.geometry import LineString
    a = audit_module()
    source = work/'south-korea.osm.pbf'
    metadata = json.loads((work/'source.json').read_text())
    if file_hash(source) != metadata['sha256']:
        raise ValueError('Source PBF checksum mismatch before extraction')
    database = work/'node-index.sqlite'
    if database.exists() or (work/'local.osm.pbf').exists():
        raise FileExistsError('Extraction requires fresh scratch paths')
    db = sqlite3.connect(database)
    try:
        db.executescript("""
            PRAGMA cache_size=-32768;
            PRAGMA temp_store=FILE;
            CREATE TABLE nodes(id INTEGER PRIMARY KEY, lon REAL, lat REAL);
            CREATE TABLE selected_ways(id INTEGER PRIMARY KEY);
            CREATE TABLE required_nodes(id INTEGER PRIMARY KEY);
        """)
        class NodeIndex(osmium.SimpleHandler):
            def __init__(self):
                super().__init__()
                self.batch = []
            def flush(self):
                db.executemany('INSERT INTO nodes VALUES(?,?,?)', self.batch)
                db.commit()
                self.batch.clear()
            def node(self, node):
                if not node.location.valid():
                    raise ValueError(f'Invalid node location: {node.id}')
                self.batch.append((node.id, node.location.lon, node.location.lat))
                if len(self.batch) >= 10000:
                    self.flush()
        index = NodeIndex()
        index.apply_file(str(source))
        index.flush()

        class SelectWays(osmium.SimpleHandler):
            def way(self, way):
                if 'highway' not in way.tags:
                    return
                refs = [n.ref for n in way.nodes]
                if len(refs) < 2:
                    raise ValueError(f'Incomplete highway way {way.id}: fewer than two nodes')
                coordinates = {}
                # Bound SQL parameter count; retain original refs separately for order/repeats.
                for offset in range(0, len(refs), 900):
                    chunk = refs[offset:offset+900]
                    query = 'SELECT id,lon,lat FROM nodes WHERE id IN ('+','.join('?' for _ in chunk)+')'
                    coordinates.update({nid:(lon,lat) for nid,lon,lat in db.execute(query,chunk)})
                missing = set(refs)-coordinates.keys()
                if missing:
                    raise ValueError(f'Missing reference nodes for highway way {way.id}: {min(missing)}')
                line = LineString([a.TO_M.transform(*coordinates[n]) for n in refs])
                # Distance to full polyline includes tangencies and crossing segments even
                # when every vertex lies outside the disk. Never clip or insert vertices.
                if line.distance(a.POINT) <= a.RADIUS:
                    db.execute('INSERT INTO selected_ways VALUES(?)',(way.id,))
                    db.executemany('INSERT OR IGNORE INTO required_nodes VALUES(?)',((n,) for n in refs))
        SelectWays().apply_file(str(source))
        db.commit()
        way_count = db.execute('SELECT count(*) FROM selected_ways').fetchone()[0]
        node_count = db.execute('SELECT count(*) FROM required_nodes').fetchone()[0]
        if not way_count:
            raise ValueError('No highway intersects the 2km radius')
        writer = osmium.SimpleWriter(str(work/'local.osm.pbf'))
        class CopySelected(osmium.SimpleHandler):
            def __init__(self):
                super().__init__()
                self.nodes_written = self.ways_written = 0
            def node(self, node):
                if db.execute('SELECT 1 FROM required_nodes WHERE id=?',(node.id,)).fetchone():
                    writer.add_node(node)
                    self.nodes_written += 1
            def way(self, way):
                if db.execute('SELECT 1 FROM selected_ways WHERE id=?',(way.id,)).fetchone():
                    writer.add_way(way)
                    self.ways_written += 1
        copy = CopySelected()
        try:
            copy.apply_file(str(source))
        finally:
            writer.close()
        if (copy.nodes_written,copy.ways_written) != (node_count,way_count):
            raise ValueError('Incomplete local extract: selected object count mismatch')
        # Decode every local reference, independently of the nationwide SQLite index.
        decoded = read_roads(work/'local.osm.pbf')
        if len(decoded['elements']) != way_count:
            raise ValueError('Incomplete local geometry reconstruction')
        metadata.update(extraction_strategy='pyosmium_three_pass_sqlite_full_way_distance',
            extraction_passes=3, node_index_storage='SQLite disk; 32MiB page cache; 10000-node insertion batches',
            radius_m=a.RADIUS, distance_crs='EPSG:5179',
            selected_way_count=way_count, required_node_count=node_count,
            local_sha256=file_hash(work/'local.osm.pbf'), way_node_references_checked=True)
        write_json(work/'source.json',metadata)
    finally:
        db.close()


def read_roads(path):
    import osmium
    class Roads(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.elements = []
        def way(self, way):
            tags = dict(way.tags)
            if 'highway' not in tags:
                return
            if len(way.nodes) < 2:
                raise ValueError(f'Incomplete highway way {way.id}: fewer than two nodes')
            if any(not n.location.valid() for n in way.nodes):
                raise ValueError(f'Missing node geometry for way {way.id}')
            self.elements.append({'type':'way', 'id':way.id, 'version':way.version,
                'timestamp':way.timestamp.isoformat(), 'tags':tags,
                'nodes':[n.ref for n in way.nodes],
                'geometry':[{'lon':n.lon,'lat':n.lat} for n in way.nodes]})
    handler = Roads()
    handler.apply_file(str(path), locations=True)
    return {'elements': handler.elements}


def analyze(work, out):
    a = audit_module()
    a.self_checks()
    # Hash all pre-existing tracked files, including prior results and GraphML.
    paths = subprocess.check_output(['git','ls-files','-z'], cwd=ROOT).decode().split('\0')
    protected = {p:file_hash(ROOT/p) for p in paths if p and (ROOT/p).is_file()}
    out.mkdir(parents=True, exist_ok=False)
    source = json.loads((work/'source.json').read_text())
    if file_hash(work/'local.osm.pbf') != source['local_sha256']:
        raise ValueError('Local extract checksum mismatch')
    _, memberships = a.existing()
    roads, _, _, _, filters = a.extract(read_roads(work/'local.osm.pbf'), memberships)
    if not roads:
        raise ValueError('No highway ways returned; stop rather than invent results')
    nearest = {m:a.closest(roads,m) for m in ('drive','drive_service','generic_highway')}
    with (ROOT/'data/processed/shelters_road_access_147.csv').open(encoding='utf-8-sig') as source_csv:
        rows = list(csv.DictReader(source_csv))
    facility = next(r for r in rows if r['시군명']=='청양군' and r['시설일련번호']=='177')
    if (float(facility['latitude']),float(facility['longitude'])) != (a.LAT,a.LON):
        raise ValueError('Facility coordinate mismatch')
    prior = json.loads((ROOT/'output/road_access_investigation/report.json').read_text())
    case = next(c for c in prior['cases'] if c['시설명']=='죽림리회관')
    neighbor = case['neighbor_county_existing_network_comparison']
    comparisons = [dict(scenario='existing_cheongyang_drive', distance_m=float(facility['road_distance_m']), way_id=facility['road_edge_osmid']),
                   dict(scenario='existing_two_county_drive_reference',distance_m=neighbor['reference_distance_m'],way_id=neighbor['nearest_edge']['osmid'])]
    comparisons += [dict(scenario='pbf_'+m,distance_m=n['distance_m'],way_id=n['way_id']) for m,n in nearest.items()]
    with (out/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['scenario','distance_m','way_id']);writer.writeheader();writer.writerows(comparisons)
    columns=['way_id','distance_m','highway','service','tracktype','access','vehicle','motor_vehicle','motorcar','bridge','surface','width','maxweight','oneway','old_cheongyang','old_buyeo','drive_rejections','drive_service_rejections','vehicle_status','all_tags']
    with (out/'roads.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader()
        for r in roads:
            row={k:r['tags'].get(k,'') for k in columns}
            row.update(way_id=r['way_id'],distance_m=r['distance_m'],old_cheongyang=r['existing_membership']['cheongyang'],old_buyeo=r['existing_membership']['buyeo'],
                drive_rejections=json.dumps(r['filter_rejections']['drive'],ensure_ascii=False),drive_service_rejections=json.dumps(r['filter_rejections']['drive_service'],ensure_ascii=False),
                vehicle_status=r['vehicle_assessment']['status'],all_tags=json.dumps(r['tags'],ensure_ascii=False))
            writer.writerow(row)
    unchanged=all(file_hash(ROOT/p)==h for p,h in protected.items())
    if not unchanged:
        raise RuntimeError('Existing tracked input changed')
    evidence={'facility':{'name':'죽림리마을회관','latitude':a.LAT,'longitude':a.LON},'source':source,
        'scope':{'facilities':1,'radius_m':2000,'distance_crs':'EPSG:5179','whole_way_geometry_preserved':True},
        'filters':filters,'osmnx_version':a.ox.__version__,'comparison':comparisons,'nearest':nearest,
        'roads':[a.public(r) for r in roads],
        'prior_boundary_evidence':case['boundary'],'prior_neighbor_evidence':neighbor,
        'limitations':['Distance is straight-line distance to original OSM geometry, not a driving route.',
            'Generic highway includes ways unsuitable for motor vehicles. Filter eligibility is not permission or proof of passage.',
            'Actual vehicle passage, bridge capacity, conditional restrictions and barriers remain unverified.',
            'Current PBF and old county graphs differ in timestamp and extent; current filter rejections alone do not prove historical exclusion causes.'],
        'preservation':{'tracked_file_sha256':protected,'unchanged':unchanged,'network_rebuilt':False}}
    write_json(out/'evidence.json',evidence)
    generic=nearest['generic_highway']
    lines=['# 죽림리마을회관 Geofabrik PBF 조사','',f"출처: {URL}",f"OSM 데이터 기준 시각: {source['data_timestamp']}",'',
        'OpenStreetMap contributors / ODbL 1.0. https://www.openstreetmap.org/copyright','',
        '| 조건 | 원본 도로까지 직선거리(m) | OSM way ID |','|---|---:|---|']
    lines += [f"| {r['scenario']} | {r['distance_m'] if r['distance_m'] is not None else '미확인'} | {r['way_id']} |" for r in comparisons]
    lines += ['',f"범용 최근접 way: `{generic['way_id']}`, `{generic.get('tags',{})}`.",
        '50m 이내 도로: '+str([r['way_id'] for r in roads if r['distance_m']<=50]),
        '기존 그래프 포함 여부: '+str(generic.get('existing_membership')),
        '현재 drive 필터 제외 조항: '+str(generic.get('filter_rejections',{}).get('drive')),'',
        '기존 1,359.55m는 청양군 drive 그래프 안에서 측정한 결과입니다. 259.89m는 군 경계를 넘어 부여군 기존 지천로를 비교한 결과로, 군별 검색 범위가 거리 차이에 영향을 줍니다.',
        '현재 PBF의 drive·drive_service·범용 도로 비교는 같은 시점·범위에서 필터 효과를 구분합니다. 원래 추출 시점의 태그를 복원하지 않았으므로, 신규 편집과 과거 필터 영향을 완전히 분리하지 못합니다.',
        '범용 OSM 도로 존재와 실제 차량 통행 가능성은 다릅니다. 물리적 통행·통행권·교량 하중·차량 제원은 미확인입니다. 구호 수송망 편입이나 전체 도로망 재구축을 승인하는 결과가 아닙니다.',
        '전체 태그, way ID, 원본 형상, 기존 두 군 그래프 포함 여부와 필터 제외 이유는 roads.csv 및 evidence.json에 기록했습니다.',
        '기존 데이터·GraphML·CSV·분석 결과는 변경하지 않았습니다.']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['download','extract','analyze'])
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    work=args.work_dir.resolve()
    # Keep downloaded data and generated outputs away from all existing data.
    if args.mode=='analyze':
        if args.output_dir is None:
            parser.error('--output-dir required for analyze')
        out=args.output_dir.resolve()
        if out==ROOT or out.is_relative_to(ROOT/'data') or out.is_relative_to(ROOT/'output'):
            parser.error('Use a fresh temporary output directory')
        analyze(work,out)
    elif args.mode=='download':
        download(work)
    else:
        extract(work)


if __name__=='__main__':
    main()
