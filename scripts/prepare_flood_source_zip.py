"""Safely flatten the 4 required 100-year flood SHP sets from one ZIP.

Do not run on untrusted file archives without format and size checks.
No source file is added to git; this is executed in RUNNER_TEMP.
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import stat
import zipfile

CODES = ("44760", "44790")
RIVERS = ("NTN", "RGN")
EXTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
EXPECTED = frozenset(
    f"RFM_SGG_{river}_{code}_100{ext}"
    for code in CODES for river in RIVERS for ext in EXTS
)
MAX_UNCOMPRESSED = 1024 * 1024 * 1024  # 1 GiB
MAX_ARCHIVE_ITEMS = 40


def unpack_source(archive: Path, destination: Path) -> list[str]:
    archive = archive.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing path: {destination}")
    if destination.is_relative_to(archive.parent) and destination == archive:
        raise ValueError("Archive and destination cannot be the same path")
    with zipfile.ZipFile(archive) as z:
        files = [entry for entry in z.infolist() if not entry.is_dir()]
        if not files or len(files) > MAX_ARCHIVE_ITEMS:
            raise ValueError(f"Expected <= {MAX_ARCHIVE_ITEMS} archive files")
        if sum(x.file_size for x in files) > MAX_UNCOMPRESSED:
            raise ValueError("Uncompressed input exceeds 1 GiB")
        found: dict[str, zipfile.ZipInfo] = {}
        for item in files:
            raw_name = item.filename.replace("\\", "/")
            path = PurePosixPath(raw_name)
            if path.is_absolute() or ".." in path.parts or ":" in path.name:
                raise ValueError(f"Unsafe archive entry: {item.filename}")
            # ZIPs may contain a parent folder; flatten required source files.
            name = path.name
            if name not in EXPECTED:
                if name.startswith(".") or name in {"README.txt", "__MACOSX"}:
                    continue
                raise ValueError(f"Unexpected file: {item.filename}")
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"Symlinks are not supported: {item.filename}")
            if name in found:
                raise ValueError(f"Duplicate source filename: {name}")
            if item.file_size <= 0:
                raise ValueError(f"Empty file: {name}")
            found[name] = item
        missing = sorted(EXPECTED - found.keys())
        if missing:
            raise ValueError(f"Missing {len(missing)} SHP components: {missing}")
        destination.mkdir(parents=True, exist_ok=False)
        for name, info in found.items():
            target = destination / name
            with z.open(info) as src, target.open("wb") as dst:
                import shutil
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    return sorted(found)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zip", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    found = unpack_source(a.zip, a.output_dir)
    print(f"Validated and extracted {len(found)} required files into {a.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
