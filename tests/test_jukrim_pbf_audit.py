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

    def test_bbox_uses_complete_ways_and_checks_references(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d);(work/'local.osm.pbf').write_bytes(b'test-only')
            m.write_json(work/'source.json',{})
            with patch('subprocess.run') as run:
                m.extract(work)
            command=run.call_args_list[0].args[0]
            self.assertEqual(command[command.index('-s')+1],'complete_ways')
            self.assertEqual(run.call_args_list[1].args[0][:2],['osmium','check-refs'])
            bbox=json.loads((work/'source.json').read_text())['extract_bbox']
            self.assertTrue(bbox[0]<126.836798762428<bbox[2])
            self.assertTrue(bbox[1]<36.3667897253441<bbox[3])


if __name__=='__main__':unittest.main()
