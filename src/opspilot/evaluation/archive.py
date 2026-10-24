"""Reproducible archives of dataset bundles (the frozen final set's release asset).

Entries are sorted, with fixed timestamps, owners and modes, so the same bundle
always gives the same archive bytes and SHA-256.
"""

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

_FIXED_MTIME = 1_767_225_600  # 2026-01-01T00:00:00Z


def archive_bundle(bundle: Path, out_dir: Path) -> tuple[Path, str]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            info = tarfile.TarInfo(f"{bundle.name}/{path.relative_to(bundle)}")
            data = path.read_bytes()
            info.size, info.mtime, info.mode = len(data), _FIXED_MTIME, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    compressed = gzip.compress(buffer.getvalue(), compresslevel=9, mtime=0)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{bundle.name}.tar.gz"
    target.write_bytes(compressed)
    digest = hashlib.sha256(compressed).hexdigest()
    (out_dir / f"{bundle.name}.tar.gz.sha256").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8"
    )
    return target, digest
