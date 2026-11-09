import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import requests
import extract


def test_retry_recovers_and_honors_retry_after(monkeypatch):
    waits = []
    monkeypatch.setattr(extract.time, 'sleep', waits.append)
    calls = []
    def request():
        calls.append(1)
        if len(calls) == 1:
            response = requests.Response()
            response.status_code = 429
            response.headers['Retry-After'] = '7'
            raise requests.HTTPError(response=response)
        return 'ok'
    assert extract.retry(request) == 'ok'
    assert waits == [7]


def test_retry_exhaustion(monkeypatch):
    monkeypatch.setattr(extract.time, 'sleep', lambda _: None)
    def fail():
        raise requests.Timeout('timeout')
    with pytest.raises(requests.Timeout):
        extract.retry(fail, attempts=2)


def test_invalid_local_boundary_is_rejected(tmp_path):
    (tmp_path / 'buyeo.geojson').write_text('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":{"type":"Point","coordinates":[126.9,36.2]}}]}')
    with pytest.raises(ValueError, match='Polygon'):
        extract.boundary('buyeo', tmp_path)
