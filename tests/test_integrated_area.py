"""Polygon-only unit and offline integration tests; never process PBF or graphs."""
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from shapely.geometry import Polygon, box, mapping, shape

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('integrated_area',ROOT/'scripts/build_integrated_area.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class IntegratedAreaTests(unittest.TestCase):
    def fixture(self,path,geometry,crs='OGC:CRS84',region='부여군'):
        data={'type':'FeatureCollection','features':[{'type':'Feature','properties':{'region':region},'geometry':mapping(geometry)}]}
        if crs is not None:data['crs']={'type':'name','properties':{'name':crs}}
        path.write_bytes(m.encoded(data));return path

    def test_crs84_and_projected_inputs_agree(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'test.geojson';g=box(126.7,36.3,126.8,36.4)
            expected,meta=m.load_boundary(self.fixture(path,g),'부여군')
            self.assertFalse(meta['crs_assumed_from_rfc7946'])
            actual,_=m.load_boundary(self.fixture(path,expected,'EPSG:5179'),'부여군')
            self.assertLess(expected.hausdorff_distance(actual),.001)

    def test_missing_crs_uses_documented_geojson_default(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.fixture(Path(d)/'test.geojson',box(126.7,36.3,126.8,36.4),None)
            _,meta=m.load_boundary(p,'부여군')
            self.assertTrue(meta['crs_assumed_from_rfc7946'])

    def test_reject_invalid_geometry_without_repair(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.fixture(Path(d)/'test.geojson',Polygon([(126.7,36.3),(126.8,36.4),(126.7,36.4),(126.8,36.3),(126.7,36.3)]))
            with self.assertRaisesRegex(ValueError,'Self-intersection'):m.load_boundary(p,'부여군')

    def test_hash_region_and_extent_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p=self.fixture(Path(d)/'test.geojson',box(126.7,36.3,126.8,36.4))
            with self.assertRaisesRegex(ValueError,'source differs'):m.load_boundary(p,'부여군','0'*64)
            with self.assertRaisesRegex(ValueError,'identity mismatch'):m.load_boundary(p,'청양군')
            self.fixture(p,box(0,0,1,1))
            with self.assertRaisesRegex(ValueError,'geographic extent'):m.load_boundary(p,'부여군')

    def test_union_overlap_is_not_double_counted_and_buffer_distance(self):
        areas=m.make_areas({'a':box(0,0,1000,1000),'b':box(500,0,1500,1000)})
        self.assertAlmostEqual(areas['union'].area,1500000)
        self.assertTrue(areas['buffer_2000m'].covers(areas['union']))
        self.assertTrue(areas['buffer_5000m'].covers(areas['buffer_2000m']))
        self.assertAlmostEqual(areas['buffer_2000m'].bounds[0],-2000)
        self.assertAlmostEqual(areas['buffer_5000m'].bounds[2],6500)

    def test_actual_inputs_offline_roundtrip_hashes_controls_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'results'
            def deny(*args,**kwargs):raise AssertionError('External network forbidden')
            with patch.object(socket.socket,'connect',deny),patch('socket.getaddrinfo',deny):
                report=m.build(ROOT,out)
            self.assertTrue(report['preservation']['unchanged'])
            self.assertEqual(len(report['outputs']),6)
            for filename,info in report['outputs'].items():
                self.assertEqual(m.sha((out/filename).read_bytes()),info['sha256'])
                geom=shape(json.loads((out/filename).read_bytes())['features'][0]['geometry'])
                self.assertTrue(geom.is_valid)
            for value in report['validation'].values():
                self.assertLess(value['roundtrip_hausdorff_m'],.01)
            self.assertTrue(all(c['membership']['union']['covered'] for c in report['control_checks']))
            road=next(c for c in report['control_checks'] if c['id']=='978430790' and c['kind']=='way')
            bridge=next(c for c in report['control_checks'] if c['id']=='978430789' and c['kind']=='way')
            self.assertTrue(road['membership']['cheongyang']['covered'])
            self.assertFalse(bridge['membership']['cheongyang']['covered'])
            self.assertFalse(bridge['membership']['buyeo']['covered'])
            repeated=m.build(ROOT,Path(d)/'results_repeat')
            self.assertEqual(report['outputs'],repeated['outputs'])
            before={p.name:m.sha(p.read_bytes()) for p in out.iterdir()}
            with self.assertRaises(FileExistsError):m.build(ROOT,out)
            self.assertEqual(before,{p.name:m.sha(p.read_bytes()) for p in out.iterdir()})


if __name__=='__main__':unittest.main()
