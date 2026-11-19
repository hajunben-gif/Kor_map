"""Acquire one nationwide Geofabrik PBF for the integrated network."""
from __future__ import annotations

from datetime import datetime, timezone
import re
import sys
from time import sleep

import requests

from scripts.jukrim_pbf_audit import UA, URL, file_hash, pbf_header

HISTORICAL_URL = "https://download.geofabrik.de/asia/south-korea-261109.osm.pbf"
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
UNAVAILABLE_HTTP = {403, 404, 410}
ATTEMPTS = 3


class PBFUnavailable(RuntimeError):
    """An absent source or a transfer failure after bounded retries."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fetch(session, url, destination):
    """Retry both headers and streamed file bodies from the same source."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with session.get(url, stream=True, timeout=(20, 120)) as response:
                status = response.status_code
                if status in UNAVAILABLE_HTTP:
                    raise PBFUnavailable(f"HTTP {status} from {url}")
                if status in TRANSIENT_HTTP:
                    failure = f"HTTP {status} from {url}"
                    delay = response.headers.get("Retry-After", "")
                elif status != 200:
                    raise RuntimeError(f"Unexpected HTTP {status} from {url}")
                else:
                    if not response.url.startswith("https://"):
                        raise ValueError("Geofabrik download did not resolve to HTTPS")
                    if destination is None:
                        content = next(response.iter_content(4096), b"")
                        match = re.match(rb"^([0-9a-fA-F]{32})(?:\s|$)", content.strip())
                        if not match:
                            raise ValueError("Invalid official MD5 file")
                        return match.group(1).decode("ascii").lower(), response.url
                    with destination.open("xb") as stream:
                        for block in response.iter_content(1024 * 1024):
                            stream.write(block)
                    return {
                        "resolved_url": response.url,
                        "http_headers": {key: response.headers[key] for key in
                                         ("Last-Modified", "ETag", "Content-Length") if key in response.headers},
                    }
        except requests.exceptions.SSLError:
            raise
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            if destination is not None:
                destination.unlink(missing_ok=True)
            failure = f"{type(exc).__name__} from {url}"
            delay = ""
        if attempt < ATTEMPTS:
            wait = int(delay) if delay.isascii() and delay.isdecimal() and len(delay) <= 6 else 5 * attempt
            if wait > 120:
                raise PBFUnavailable(f"{failure}; server requested a retry after {wait}s")
            print(f"PBF download attempt {attempt}/{ATTEMPTS}: {failure}; retry in {wait}s", file=sys.stderr)
            sleep(wait)
    raise PBFUnavailable(f"{failure} after {ATTEMPTS} attempts")


def acquire_pbf(work, reference, *, historical, allow_latest=False):
    """Download to a private scratch directory and verify before analysis."""
    pbf = work / "south-korea.osm.pbf"
    started = utc_now()
    history = []
    with requests.Session() as session:
        session.headers["User-Agent"] = UA
        if historical:
            try:
                transfer = fetch(session, HISTORICAL_URL, pbf)
            except PBFUnavailable as exc:
                if not allow_latest:
                    raise PBFUnavailable(f"Historical PBF unavailable: {exc}. Latest fallback is disabled.") from exc
                history.append({"url": HISTORICAL_URL, "status": "unavailable", "reason": str(exc)})
                print(f"Historical PBF unavailable; operator opted in to latest PBF: {exc}", file=sys.stderr)
            else:
                sha256, size = file_hash(pbf), pbf.stat().st_size
                if sha256 != reference["sha256"] or size != reference["bytes"]:
                    raise ValueError("Historical PBF SHA-256 or size differs from the preserved reference")
                header = pbf_header(pbf)
                if header["data_timestamp"] != reference["data_timestamp"]:
                    raise ValueError("Historical PBF timestamp differs from the preserved reference")
                return pbf, {
                    **transfer, **header, "source_url": HISTORICAL_URL, "selection": "historical",
                    "verification": "archived SHA-256, size and timestamp",
                    "sha256": sha256, "bytes": size, "started_at_utc": started,
                    "finished_at_utc": utc_now(), "attempted_sources": [
                        {"url": HISTORICAL_URL, "status": "verified"}
                    ],
                }
        expected_md5, checksum_url = fetch(session, URL + ".md5", None)
        transfer = fetch(session, URL, pbf)
    if file_hash(pbf, "md5") != expected_md5:
        raise ValueError("Latest PBF differs from the official MD5")
    header = pbf_header(pbf)
    history.append({"url": URL, "status": "verified"})
    return pbf, {
        **transfer, **header, "source_url": URL, "checksum_url": checksum_url,
        "selection": "latest_fallback" if historical else "latest",
        "verification": "official MD5 and PBF header",
        "md5": expected_md5, "sha256": file_hash(pbf), "bytes": pbf.stat().st_size,
        "started_at_utc": started, "finished_at_utc": utc_now(), "attempted_sources": history,
    }
