"""Check Windows-style checkout of the verified offline map boundaries."""

import hashlib
import json
from functools import partial
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = Path('data/20261109T045434949410Z')
VALIDATION = Path('output/integrated_area/boundaries_20261113/validation.json')


def test_git_checkout_preserves_verified_boundary_bytes_with_autocrlf(tmp_path):
    qa = json.loads((ROOT / VALIDATION).read_bytes())
    empty_attributes = tmp_path / 'empty-attributes'
    empty_attributes.touch()
    git = partial(subprocess.check_output, cwd=tmp_path)
    git_args = ['git', '-c', 'core.autocrlf=true', '-c', f'core.attributesFile={empty_attributes}']
    git([*git_args, 'init', '--quiet', '--template='])
    attributes = tmp_path / '.gitattributes'
    attributes.write_bytes(b'')

    originals = {}
    for county in ('buyeo', 'cheongyang'):
        path = (ARCHIVE / county / 'boundary.geojson').as_posix()
        blob = subprocess.check_output(['git', 'cat-file', 'blob', f'HEAD:{path}'], cwd=ROOT)
        assert hashlib.sha256(blob).hexdigest() == qa['sources'][county]['sha256']
        originals[path] = blob
        oid = git([*git_args, 'hash-object', '-w', '--stdin'], input=blob).decode().strip()
        git([*git_args, 'update-index', '--add', '--cacheinfo', f'100644,{oid},{path}'])

    git([*git_args, 'checkout-index', '--', *originals])
    for path, blob in originals.items():
        crlf = (tmp_path / path).read_bytes()
        assert crlf != blob and crlf == blob.replace(b'\n', b'\r\n')

    attributes.write_bytes((ROOT / '.gitattributes').read_bytes())
    git([*git_args, 'checkout-index', '--force', '--', *originals])
    for path, blob in originals.items():
        assert (tmp_path / path).read_bytes() == blob, path
