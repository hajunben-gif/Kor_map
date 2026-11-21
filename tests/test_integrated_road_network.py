"""Offline tests. Synthetic geometry is used only in explicitly named test fixtures."""
from __future__ import annotations

from collections import deque
import json

import networkx as nx
import osmium
import pytest
import requests
from shapely.geometry import box

from scripts import analyze_road_access as access
from scripts import build_integrated_road_network as integrated
from scripts import integrated_pbf_source as pbf_source
from scripts import integrated_road_graph as roads
from scripts.jukrim_pbf_audit import file_hash

ROOT = integrated.ROOT


def write_pbf(path, nodes, ways, tags=None):
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", "2026-01-01T00:00:00Z")
    with osmium.SimpleWriter(path, header=header) as writer:
        for node, coord in sorted(nodes.items()):
            writer.add_node(osmium.osm.mutable.Node(id=node, location=coord, tags=(tags or {}).get(node, {})))
        for way, (refs, attrs) in sorted(ways.items()):
            writer.add_way(osmium.osm.mutable.Way(id=way, nodes=refs, tags=attrs))


def wgs(x, y):
    return access.TO_WGS84.transform(x, y)


def test_synthetic_way_selection_direction_and_simplification(tmp_path):
    x, y = access.TO_METRES.transform(126.84, 36.37)
    area = box(x - 200, y - 200, x + 200, y + 200)
    counties = {"a": box(x - 180, y - 180, x, y + 180),
                "b": box(x, y - 180, x + 180, y + 180)}
    nodes = {
        1: wgs(x-300, y), 2: wgs(x+300, y), 3: wgs(x+350, y),
        4: wgs(x-100, y+20), 5: wgs(x, y+40), 6: wgs(x+100, y+20),
        7: wgs(x, y-100), 8: wgs(x, y+130), 9: wgs(x+100, y+100),
        10: wgs(x+25, y+100), 11: wgs(x+125, y+100), 12: wgs(x+50, y+100),
        13: wgs(x-75, y-100), 14: wgs(x-75, y-100),
    }
    ways = {
        100: ([1, 2], {"highway": "residential", "bridge": "yes", "hgv:conditional": "no @ (wet)"}),
        101: ([2, 3], {"highway": "residential"}),
        102: ([4, 5, 6], {"highway": "residential", "oneway": "-1"}),
        103: ([7, 8], {"highway": "residential"}),  # crosses bridge without a shared node
        104: ([8, 9], {"highway": "service"}),
        105: ([10, 11, 12, 10], {"highway": "residential", "junction": "roundabout"}),
        106: ([4, 5], {"highway": "tertiary"}),  # parallel to part of way 102
        107: ([13, 14], {"highway": "residential"}),  # same coordinates, different IDs
    }
    path = tmp_path / "synthetic.osm.pbf"
    write_pbf(path, nodes, ways, tags={5: {"barrier": "gate"}})
    selected, extracted = roads.read_pbf(path, area, tmp_path)
    assert extracted["intersecting_highway_ways"] == 7
    rejected = roads.drive_rejections(selected)
    assert rejected[104] and 101 not in selected
    raw, simple, audit = roads.build_graphs(selected, rejected, area, counties, path)
    assert isinstance(raw, nx.MultiDiGraph) and raw.graph["crs"] == "EPSG:4326"
    assert set(audit["drive_ways_touching_outer_boundary"]) == {100}
    assert audit["drive_ways_touching_outer_boundary"][100]["all_vertices_outside"]
    assert audit["source_external_neighbors_at_retained_nodes"]["2"]["omitted_drive_ways"] == [101]
    assert {100, 102} <= set(audit["drive_ways_overlapping_both_counties"])
    assert all(graph.has_edge(1, 2) and graph.has_edge(2, 1) for graph in (raw, simple))
    assert not nx.has_path(simple, 1, 8)
    assert simple.has_edge(6, 5) and not simple.has_edge(5, 6)
    assert len(simple.get_edge_data(5, 4)) == 2
    assert json.loads(simple.nodes[5]["osm_tags"]) == {"barrier": "gate"}
    assert json.loads(simple[1][2][0]["osm_tags"])["hgv:conditional"] == "no @ (wet)"
    assert audit["original_directed_segments_preserved"]
    assert audit["retained_node_wcc_and_scc_partitions_preserved"]
    for stage in ("raw", "simplified"):
        assert audit["statistics"][stage]["zero_length_source_segments"] == 1
    hit = integrated.spatial_index(simple).nearest(integrated.Point(x-160, y+3))
    assert hit["distance"] == pytest.approx(3, abs=.02)
    assert hit["tie_count"] == 2
    assert hit["component"]["directed_edge_count"] == 2
    source_audit = {row["osm_way_id"]: row for row in roads.audit_source_ways(selected, rejected, raw, simple, audit)}
    assert source_audit[100]["drive_way_all_vertices_outside"]
    assert source_audit[100]["final_directed_segments"] == 2
    assert source_audit[102]["expected_directed_segments"] == 2
    assert source_audit[104]["status"] == "filtered"
    assert source_audit[104]["expected_directed_segments"] == 0
    simple.remove_edge(1, 2)
    with pytest.raises(ValueError, match="source way 100"):
        roads.audit_source_ways(selected, rejected, raw, simple, audit)


def test_preserved_jukrim_source_replay_has_shared_osm_nodes():
    result = integrated.replay_jukrim(ROOT)
    assert result["new_nearest"]["way_id"] == 978430790
    assert result["new_nearest"]["distance_m"] == pytest.approx(9.514797320787055, abs=1e-8)
    assert result["reference_omission_resolved_in_tested_scope"]
    assert result["scope"] == "preserved_jukrim_subset"
    for stage in result["stages"].values():
        assert all(stage["edges_by_way"].values())
        assert stage["access_bridge_shared_osm_nodes"] == [9053552621]
        assert stage["bridge_trunk_shared_osm_nodes"] == [1781315489]
        assert all(stage["directed_reachability"].values())
        assert all(stage["trunk_contact_movements"].values())


def test_synthetic_full_run_writes_valid_outputs_and_all_original_facilities(tmp_path):
    original, columns, prior = integrated.load_facilities(ROOT)
    nodes, ways = {}, {}
    for number, row in enumerate(original, 1):
        if not row["latitude"]:
            continue
        x, y = access.TO_METRES.transform(float(row["longitude"]), float(row["latitude"]))
        start = number * 10
        offset = 350 if access.identity(row) == ("부여군", "167") else 10
        nodes[start], nodes[start + 1], nodes[start + 2] = (
            wgs(x-20, y+offset), wgs(x, y+offset), wgs(x+20, y+offset))
        ways[number] = ([start, start + 1, start + 2], {"highway": "residential"})
    path = tmp_path / "synthetic-coverage.osm.pbf"
    write_pbf(path, nodes, ways, tags={min(nodes): {"barrier": "gate"}})
    target = tmp_path / "results"
    before = integrated.input_hashes(ROOT)
    meta = integrated.build(ROOT, path, target, checksum=file_hash(path), source="synthetic unit test")
    assert integrated.input_hashes(ROOT) == before
    assert not meta["source"]["same_as_archived_pbf"]
    assert meta["source"]["data_timestamp"] == "2026-01-01T00:00:00Z"
    assert meta["facilities"]["matched_facilities"] == 146
    assert meta["facilities"]["thresholds_m"][300] == {"before": 6, "after": 1}
    assert meta["facilities"]["resolved_300m"] == 6
    assert meta["graph"]["statistics"]["raw"]["undirected_length_m"] == pytest.approx(146*40, abs=2)
    comparison, _ = integrated.read_csv(target / "facilities_comparison_147.csv")
    assert [{k: row[k] for k in columns} for row in comparison] == original
    assert len({access.identity(row) for row in comparison}) == 147
    excluded = [row for row in comparison if row["road_access_status"] != "matched"]
    assert len(excluded) == 1
    assert excluded[0]["시설명"] == "대동마을회관" and excluded[0]["road_distance_m"] == ""
    assert all(row["road_distance_crs"] == "EPSG:5179" for row in comparison)
    details, _ = integrated.read_csv(target / "outliers_existing_6.csv")
    assert {row["시설일련번호"] for row in details} == set(integrated.REVIEW)
    assert all(row["old_road_distance_m"] == prior[access.identity(row)]["road_distance_m"] for row in details)
    newly_detected, _ = integrated.read_csv(target / "outliers_newly_detected.csv")
    assert [access.identity(row) for row in newly_detected] == [("부여군", "167")]
    assert float(newly_detected[0]["road_distance_m"]) == pytest.approx(350, abs=.02)
    assert {p.name for p in target.iterdir()} == {
        "raw.graphml", "simplified.graphml", "network.gpkg", "metadata.json", "jukrim.json",
        "facilities_comparison_147.csv", "outliers_existing_6.csv", "outliers_newly_detected.csv",
        "report.md", "way_audit.csv",
    }
    way_audit, _ = integrated.read_csv(target / "way_audit.csv")
    assert meta["verification"]["compared_source_highway_ways"] == len(way_audit) == 146
    assert meta["verification"]["missing_drive_way_segments"] == 0
    assert all(row["expected_directed_segments"] == row["final_directed_segments"] == "4" for row in way_audit)
    with pytest.raises(FileExistsError):
        integrated.build(ROOT, path, target, checksum=file_hash(path), source="synthetic unit test")
    assert all(file_hash(target / name) == sha for name, sha in meta["output_sha256"].items())


@pytest.mark.parametrize("problem", ["missing", "bad_sha", "truncated", "missing_ref"])
def test_synthetic_bad_pbf_never_publishes_success(tmp_path, problem):
    source = tmp_path / "invalid.osm.pbf"
    if problem == "truncated":
        source.write_bytes(b"\x00\x00\x00\x05bad")
    if problem in ("missing_ref", "bad_sha"):
        write_pbf(source, {10: (126.84, 36.37)}, {100: ([10, 11], {"highway": "residential"})})
    checksum = "0" * 64 if problem == "bad_sha" else file_hash(source) if source.exists() else None
    destination = tmp_path / "results"
    expected = {
        "missing": (FileNotFoundError, "invalid.osm.pbf"),
        "bad_sha": (ValueError, "SHA-256 mismatch"),
        "truncated": (ValueError, "Invalid PBF header"),
        "missing_ref": (ValueError, "missing references"),
    }[problem]
    with pytest.raises(expected[0], match=expected[1]):
        integrated.build(ROOT, source, destination, checksum=checksum, source="synthetic unit test")
    assert not destination.exists()


class PBFResponse:
    def __init__(self, status, body=b"", error=None):
        self.status_code = status
        self.body = body
        self.error = error
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk_size):
        yield self.body
        if self.error:
            raise self.error


def mock_geofabrik(monkeypatch, responses):
    class Session:
        def __init__(self):
            self.requests = []
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            self.requests.append(url)
            response = responses.popleft()
            if isinstance(response, Exception):
                raise response
            response.url = url
            return response

    session = Session()
    monkeypatch.setattr(pbf_source.requests, "Session", lambda: session)
    delays = []
    monkeypatch.setattr(pbf_source, "sleep", delays.append)
    return session.requests, delays


@pytest.fixture
def download_snapshot(tmp_path):
    path = tmp_path / "test-source.osm.pbf"
    write_pbf(path, {1: (126.84, 36.37), 2: (126.85, 36.37)}, {100: ([1, 2], {"highway": "residential"})})
    reference = {
        "sha256": file_hash(path), "bytes": path.stat().st_size, "data_timestamp": "2026-01-01T00:00:00Z"
    }
    return path.read_bytes(), reference, file_hash(path, "md5")


def test_download_prefers_verified_historical_even_if_latest_allowed(tmp_path, monkeypatch, download_snapshot):
    payload, reference, _ = download_snapshot
    calls, delays = mock_geofabrik(monkeypatch, deque([PBFResponse(200, payload)]))
    path, metadata = pbf_source.acquire_pbf(tmp_path, reference, historical=True, allow_latest=True)
    assert calls == [pbf_source.HISTORICAL_URL]
    assert delays == []
    assert path.read_bytes() == payload and metadata["sha256"] == reference["sha256"]
    assert metadata["selection"] == "historical"
    assert metadata["data_timestamp"] == reference["data_timestamp"]


def test_download_historical_absent_requires_explicit_latest_opt_in(tmp_path, monkeypatch, download_snapshot):
    _, reference, _ = download_snapshot
    calls, _ = mock_geofabrik(monkeypatch, deque([PBFResponse(404)]))
    with pytest.raises(pbf_source.PBFUnavailable, match="Latest fallback is disabled"):
        pbf_source.acquire_pbf(tmp_path, reference, historical=True)
    assert calls == [pbf_source.HISTORICAL_URL]
    assert not (tmp_path / "south-korea.osm.pbf").exists()


def test_download_historical_transport_retries_then_verifies_opted_in_latest(tmp_path, monkeypatch, download_snapshot):
    payload, reference, md5 = download_snapshot
    responses = deque([
        PBFResponse(200, b"partial", error=requests.exceptions.ChunkedEncodingError()),
        requests.Timeout(),
        PBFResponse(503),
        PBFResponse(200, f"{md5}  south-korea-latest.osm.pbf\n".encode("ascii")),
        PBFResponse(200, payload),
    ])
    calls, delays = mock_geofabrik(monkeypatch, responses)
    path, metadata = pbf_source.acquire_pbf(tmp_path, reference, historical=True, allow_latest=True)
    assert calls == [pbf_source.HISTORICAL_URL] * 3 + [pbf_source.URL + ".md5", pbf_source.URL]
    assert delays == [5, 10]
    assert path.read_bytes() == payload
    assert metadata["selection"] == "latest_fallback"
    assert metadata["md5"] == md5 and metadata["sha256"] == reference["sha256"]
    assert metadata["attempted_sources"][0]["status"] == "unavailable"


@pytest.mark.parametrize("mismatch", ["sha256", "bytes", "data_timestamp"])
def test_download_mismatched_historical_never_uses_latest(tmp_path, monkeypatch, download_snapshot, mismatch):
    payload, reference, _ = download_snapshot
    reference[mismatch] = {"sha256": "0" * 64, "bytes": 0, "data_timestamp": "2000-01-01T00:00:00Z"}[mismatch]
    calls, _ = mock_geofabrik(monkeypatch, deque([PBFResponse(200, payload)]))
    with pytest.raises(ValueError, match="Historical PBF"):
        pbf_source.acquire_pbf(tmp_path, reference, historical=True, allow_latest=True)
    assert calls == [pbf_source.HISTORICAL_URL]


def test_download_rejects_latest_when_official_md5_differs(tmp_path, monkeypatch, download_snapshot):
    payload, reference, _ = download_snapshot
    responses = deque([PBFResponse(404), PBFResponse(200, b"0" * 32), PBFResponse(200, payload)])
    calls, _ = mock_geofabrik(monkeypatch, responses)
    with pytest.raises(ValueError, match="official MD5"):
        pbf_source.acquire_pbf(tmp_path, reference, historical=True, allow_latest=True)
    assert calls == [pbf_source.HISTORICAL_URL, pbf_source.URL + ".md5", pbf_source.URL]
