"""Synthetic fixtures test code behavior, never serve as audit evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
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
            work=Path(d);pbf=work/'local.osm.pbf'
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
                m.analyze(work,work/'results')
            e=json.loads((work/'results/evidence.json').read_text())
            self.assertEqual(e['nearest']['drive']['way_id'],9000000001)
            self.assertEqual(e['nearest']['drive_service']['way_id'],9000000000)
            self.assertEqual(e['nearest']['generic_highway']['way_id'],9000000002)
            self.assertTrue(e['preservation']['unchanged'])
            self.assertEqual(e['scope']['facilities'],1)
            self.assertEqual({p.suffix for p in (work/'results').iterdir()},{'.json','.csv','.md'})
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
            metadata=json.loads((work/'source.json').read_text())
            self.assertEqual(metadata['selected_way_count'],2)
            self.assertTrue(metadata['way_node_references_checked'])
            self.assertEqual(metadata['source_url'],m.URL)
            self.assertEqual(metadata['data_timestamp'],'2026-01-01T00:00:00Z')

    def test_extraction_missing_reference_fails_without_success_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d);self.make_source(work,missing=True)
            with self.assertRaisesRegex(ValueError,'Missing reference nodes.*104'):
                m.extract(work)
            metadata=json.loads((work/'source.json').read_text())
            self.assertNotIn('local_sha256',metadata)
            self.assertFalse((work/'local.osm.pbf').exists())

    def test_source_corruption_stops_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d);self.make_source(work)
            with (work/'south-korea.osm.pbf').open('ab') as f:f.write(b'corruption')
            with self.assertRaisesRegex(ValueError,'Source PBF checksum mismatch'):
                m.extract(work)


if __name__=='__main__':unittest.main()
