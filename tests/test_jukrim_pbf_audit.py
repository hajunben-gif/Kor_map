"""Synthetic fixtures test code behavior, never serve as audit evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import osmium

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pbf_audit',ROOT/'scripts/jukrim_pbf_audit.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class PBFAuditTests(unittest.TestCase):
    def test_official_download_failure_stops_without_fallback(self):
        with tempfile.TemporaryDirectory() as d, patch('requests.Session') as session:
            session.return_value.get.return_value.status_code=503
            with self.assertRaisesRegex(RuntimeError,'checksum download failed'):
                m.download(Path(d)/'download')
            self.assertEqual(session.return_value.get.call_count,1)
            self.assertEqual(session.return_value.get.call_args.args[0],m.URL+'.md5')

    def test_real_pbf_decode_filters_outputs_and_preservation(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d)/'work';work.mkdir();pbf=work/'local.osm.pbf'
            writer=osmium.SimpleWriter(str(pbf))
            # Near service, farther residential, closest private track.
            for i,lat in enumerate([36.36680,36.36780,36.36679]):
                for j,lon in enumerate([126.8367,126.8369]):
                    writer.add_node(osmium.osm.mutable.Node(id=10+i*2+j,location=(lon,lat)))
                tags={'highway':['service','residential','track'][i]}
                if i==2:tags['access']='private'
                writer.add_way(osmium.osm.mutable.Way(id=9000000000+i,nodes=[10+i*2,11+i*2],tags=tags))
            writer.close()
            m.write_json(work/'source.json',{'source_url':m.URL,'data_timestamp':'2026-01-01T00:00:00Z','local_sha256':m.file_hash(pbf),'test_fixture':True})
            with patch('requests.sessions.Session.request',side_effect=AssertionError('Network forbidden during offline analysis')):
                m.analyze(work,Path(d)/'results')
            e=json.loads((Path(d)/'results/evidence.json').read_text(encoding='utf-8'))
            self.assertEqual(e['nearest']['drive']['way_id'],9000000001)
            self.assertEqual(e['nearest']['drive_service']['way_id'],9000000000)
            self.assertEqual(e['nearest']['generic_highway']['way_id'],9000000002)
            self.assertTrue(e['preservation']['unchanged'])
            self.assertEqual(e['scope']['facilities'],1)
            self.assertEqual({p.suffix for p in (Path(d)/'results').iterdir()},{'.json','.csv','.md'})
            self.assertTrue(all(not any(r['existing_membership'].values()) for r in e['roads']))

    def test_missing_way_node_fails_instead_of_shortening_geometry(self):
        with tempfile.TemporaryDirectory() as d:
            xml=Path(d)/'missing.osm'
            xml.write_text('<osm version="0.6"><node id="1" lat="36.36" lon="126.83"/><way id="2"><nd ref="1"/><nd ref="999"/><tag k="highway" v="service"/></way></osm>')
            with self.assertRaises((ValueError,osmium.InvalidLocationError)):
                m.read_roads(xml)

    def make_source(self, work, missing=False):
        a=m.audit_module()
        coords={1:(-3000,0),2:(3000,0),3:(0,3000),
                4:(-3000,2100),5:(3000,2100),6:(1999,0),7:(2200,0)}
        writer=osmium.SimpleWriter(str(work/'south-korea.osm.pbf'))
        for nid,(dx,dy) in coords.items():
            lon,lat=a.TO_WGS.transform(a.POINT.x+dx,a.POINT.y+dy)
            writer.add_node(osmium.osm.mutable.Node(id=nid,location=(lon,lat)))
        ways=[(101,[2,1,3,2],{'highway':'service','name':'순서 보존','bridge':'yes','custom':'retained'}),
              (102,[4,5],{'highway':'residential'}),
              (103,[6,7],{'highway':'track'})]
        if missing:
            ways.append((104,[1,999],{'highway':'service'}))
        for wid,refs,tags in ways:
            writer.add_way(osmium.osm.mutable.Way(id=wid,nodes=refs,tags=tags))
        writer.close()
        m.write_json(work/'source.json',{'source_url':m.URL,'sha256':m.file_hash(work/'south-korea.osm.pbf'),
            'md5':m.file_hash(work/'south-korea.osm.pbf','md5'),'data_timestamp':'2026-01-01T00:00:00Z','test_fixture':True})

    def test_crossing_without_inside_vertices_and_complete_order(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d);self.make_source(work)
            with patch('subprocess.run',side_effect=AssertionError('No system CLI allowed')), patch('requests.sessions.Session.request',side_effect=AssertionError('No network')):
                m.extract(work)
            roads={r['id']:r for r in m.read_roads(work/'local.osm.pbf')['elements']}
            self.assertEqual(set(roads),{101,103})
            self.assertEqual(roads[101]['nodes'],[2,1,3,2])
            self.assertEqual(roads[101]['tags'],{'highway':'service','name':'순서 보존','bridge':'yes','custom':'retained'})
            self.assertEqual(len(roads[101]['geometry']),4)
            metadata=json.loads((work/'source.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['selected_way_count'],2)
            self.assertTrue(metadata['way_node_references_checked'])
            self.assertEqual(metadata['source_url'],m.URL)
            self.assertEqual(metadata['data_timestamp'],'2026-01-01T00:00:00Z')

    def test_extraction_missing_reference_fails_without_success_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d);self.make_source(work,missing=True)
            with self.assertRaisesRegex(ValueError,'unknown=1'):
                m.extract(work)
            metadata=json.loads((work/'source.json').read_text(encoding='utf-8'))
            self.assertNotIn('local_sha256',metadata)
            self.assertFalse((work/'local.osm.pbf').exists())

    def test_incomplete_highway_scope_and_no_failed_index_reuse(self):
        # Missing geometry stays unknown even if every known vertex is distant.
        cases = [([1], 'outside'), ([6], 'relevant'), ([], 'unknown'),
                 ([999], 'unknown'), ([1,999], 'unknown'),
                 ([6,999], 'relevant'), ([1,2,999], 'relevant')]
        for refs, expected in cases:
            with self.subTest(refs=refs), tempfile.TemporaryDirectory() as d:
                work=Path(d);self.make_source(work)
                original=work/'south-korea.osm.pbf'
                replacement=work/'replacement.osm.pbf'
                writer=osmium.SimpleWriter(str(replacement))
                class Copy(osmium.SimpleHandler):
                    def node(self,node):writer.add_node(node)
                    def way(self,way):writer.add_way(way)
                Copy().apply_file(m.osmium_path(original))
                writer.add_way(osmium.osm.mutable.Way(id=842288033,nodes=refs,
                    tags={'highway':'service','note':'retain evidence'}))
                # A second anomaly verifies the scan reports all objects before failing.
                writer.add_way(osmium.osm.mutable.Way(id=842288034,nodes=[3],tags={'highway':'track'}))
                writer.close();replacement.replace(original)
                meta=json.loads((work/'source.json').read_text(encoding='utf-8'))
                meta['sha256']=m.file_hash(original);m.write_json(work/'source.json',meta)
                if expected=='outside':
                    m.extract(work)
                    roads={r['id']:r for r in m.read_roads(work/'local.osm.pbf')['elements']}
                    self.assertEqual(set(roads),{101,103})
                    self.assertEqual(roads[101]['nodes'],[2,1,3,2])
                    a=m.audit_module()
                    for point,delta in zip(roads[101]['geometry'],[(3000,0),(-3000,0),(0,3000),(3000,0)]):
                        x,y=a.TO_M.transform(point['lon'],point['lat'])
                        self.assertAlmostEqual(x,a.POINT.x+delta[0],delta=0.02)
                        self.assertAlmostEqual(y,a.POINT.y+delta[1],delta=0.02)
                else:
                    with self.assertRaisesRegex(ValueError,'require review'):m.extract(work)
                    self.assertFalse((work/'local.osm.pbf').exists())
                    self.assertNotIn('local_sha256',json.loads((work/'source.json').read_text(encoding='utf-8')))
                issues=[json.loads(line) for line in (work/'incomplete-highways.jsonl').read_text(encoding='utf-8').splitlines()]
                self.assertEqual(len(issues),2)
                self.assertEqual(issues[0]['classification'],expected)
                self.assertEqual(issues[0]['nodes'],refs)
                self.assertEqual(issues[0]['tags']['note'],'retain evidence')
                self.assertEqual(issues[1]['classification'],'outside')
                before=m.file_hash(work/'node-index.sqlite')
                with self.assertRaisesRegex(FileExistsError,'fresh scratch'):m.extract(work)
                self.assertEqual(before,m.file_hash(work/'node-index.sqlite'))

    def test_stale_index_and_failure_marker_refused(self):
        for name in ['node-index.sqlite','node-index.sqlite-wal','FAILED_DO_NOT_USE.txt']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                work=Path(d);self.make_source(work)
                stale=work/name;stale.write_bytes(b'failed previous run')
                with self.assertRaisesRegex(FileExistsError,'fresh scratch'):m.extract(work)
                self.assertEqual(stale.read_bytes(),b'failed previous run')

    def test_source_corruption_stops_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d);self.make_source(work)
            with (work/'south-korea.osm.pbf').open('ab') as f:f.write(b'corruption')
            with self.assertRaisesRegex(ValueError,'Source PBF checksum mismatch'):
                m.extract(work)


    def local_fixture(self, folder):
        folder.mkdir()
        header=osmium.io.Header()
        header.set('osmosis_replication_timestamp','2026-01-01T00:00:00Z')
        path=folder/'\uD55C\uAE00 sample.osm.pbf'
        writer=osmium.SimpleWriter(str(folder/'fixture.osm.pbf'),header=header)
        writer.add_node(osmium.osm.mutable.Node(id=1,location=(126.8367,36.3668)))
        writer.add_node(osmium.osm.mutable.Node(id=2,location=(126.8369,36.3668)))
        writer.add_way(osmium.osm.mutable.Way(id=9000000005,nodes=[1,2],tags={'highway':'residential'}))
        writer.close()
        (folder/'fixture.osm.pbf').rename(path)
        return path

    def test_local_pipeline_no_copy_no_network_and_preservation(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);pbf=self.local_fixture(base/'source folder')
            before=(pbf.read_bytes(),pbf.stat().st_mtime_ns)
            work=base/'\uC791\uC5C5 folder';out=base/'\uACB0\uACFC folder'
            with patch('socket.socket.connect',side_effect=AssertionError('Network forbidden')), patch('requests.sessions.Session.request',side_effect=AssertionError('Network forbidden')):
                m.prepare_local(work,pbf)
                self.assertEqual([p.name for p in work.iterdir()],['source.json'])
                meta=json.loads((work/'source.json').read_text(encoding='utf-8'))
                self.assertEqual(meta['source_path'],str(pbf.resolve()))
                self.assertEqual(meta['sha256'],m.file_hash(pbf))
                self.assertEqual(meta['bytes'],pbf.stat().st_size)
                self.assertEqual(meta['data_timestamp'],'2026-01-01T00:00:00Z')
                m.extract(work)
                m.analyze(work,out)
            self.assertEqual(before,(pbf.read_bytes(),pbf.stat().st_mtime_ns))
            evidence=json.loads((out/'evidence.json').read_text(encoding='utf-8'))
            self.assertEqual(evidence['source']['source_path'],str(pbf.resolve()))
            self.assertTrue(evidence['preservation']['unchanged'])
            self.assertIn(str(pbf.resolve()),(out/'report.md').read_text(encoding='utf-8'))
            with self.assertRaises(FileExistsError):m.prepare_local(work,pbf)
            with self.assertRaises(FileExistsError):m.extract(work)
            with self.assertRaises(FileExistsError):m.analyze(work,out)

    def test_cli_windows_style_path_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);pbf=self.local_fixture(base/'cli folder')
            command=[sys.executable,str(ROOT/'scripts/jukrim_pbf_audit.py'),'prepare-local','--work-dir',str(base/'cli work'),'--pbf-path',str(pbf)]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('already exists',result.stderr)
            self.assertNotIn('Traceback',result.stderr)

    def test_invalid_local_inputs_and_external_directories(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);work=base/'work'
            with self.assertRaises(FileNotFoundError):m.prepare_local(work,base/'missing.osm.pbf')
            bad=base/'bad.osm.pbf';bad.write_bytes(b'not a PBF')
            with self.assertRaisesRegex(ValueError,'Invalid PBF header'):m.prepare_local(work,bad)
            writer=osmium.SimpleWriter(str(base/'no-time.osm.pbf'));writer.close()
            with self.assertRaisesRegex(ValueError,'timestamp'):m.prepare_local(work,base/'no-time.osm.pbf')
            self.assertFalse(work.exists())
            with self.assertRaisesRegex(ValueError,'outside'):m.prepare_local(ROOT/'scratch',bad)
            pbf=self.local_fixture(base/'source')
            m.prepare_local(work,pbf)
            with pbf.open('ab') as f:f.write(b'corrupt')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):m.extract(work)

    def test_download_success_legacy_source_compatible(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);pbf=self.local_fixture(base/'source')
            from unittest.mock import MagicMock
            checksum=MagicMock(status_code=200,text=m.file_hash(pbf,'md5')+'  sample.pbf')
            response=MagicMock(status_code=200,headers={})
            response.__enter__.return_value=response
            response.iter_content.return_value=[pbf.read_bytes()]
            with patch('requests.Session') as session:
                session.return_value.get.side_effect=[checksum,response]
                m.download(base/'download')
            meta=json.loads((base/'download/source.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['source_type'],'download')
            self.assertEqual(meta['source_url'],m.URL)
            # Legacy metadata without source_type must still select downloaded PBF.
            meta.pop('source_type');m.write_json(base/'download/source.json',meta)
            with patch('requests.sessions.Session.request',side_effect=AssertionError('No network')):
                m.extract(base/'download')
            self.assertTrue((base/'download/local.osm.pbf').exists())


if __name__=='__main__':unittest.main()
