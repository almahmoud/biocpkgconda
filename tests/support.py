import hashlib
import importlib.util
import io
import json
import tarfile
import unittest
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

from biocpkgconda import config, metadata

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def channel():
    """channel.toml with the Bioconductor and R versions of the fixtures."""
    return replace(config.load_channel(ROOT / "channel.toml"), bioc_version="3.23", r_version="4.6")


DESEQ2_BIOC = ("S4Vectors", "IRanges", "GenomicRanges", "SummarizedExperiment", "Biobase", "BiocParallel",
               "MatrixGenerics")
DESEQ2_CRAN = ("matrixStats", "locfit", "ggplot2", "RcppArmadillo", "generics")


def _stubs(names):
    return "\n".join(f"Package: {n}\nVersion: 1.0\nMD5sum: {'0' * 32}\nNeedsCompilation: no\n" for n in names)


def sources(bioc=DESEQ2_BIOC, cran=DESEQ2_CRAN) -> metadata.Sources:
    """Packages from the fixtures plus stub packages with the given names."""
    bioc_packages = metadata.parse_bioc(fixture("PACKAGES.bioc"), "3.23", "bioc")
    bioc_packages |= metadata.parse_bioc(_stubs(bioc), "3.23", "bioc")
    cran_packages = metadata.parse_cran(fixture("PACKAGES.cran"))
    cran_packages |= metadata.parse_cran(_stubs(cran))
    return metadata.Sources(bioc_packages, cran_packages)


def tarball(package: str, files: dict[str, str | bytes]) -> bytes:
    """A gzipped source tarball with ``files`` under ``<package>/``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(f"{package}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


requires_zstandard = unittest.skipUnless(importlib.util.find_spec("zstandard"),
                                         "zstandard is not installed (pip install -r requirements.txt)")


@dataclass(frozen=True)
class Symlink:
    target: str


def zst_tar(files: dict) -> bytes:
    """A zstd-compressed tar archive of ``files``: bytes, ``Symlink`` or None for a directory."""
    import zstandard

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif isinstance(content, Symlink):
                info.type, info.linkname = tarfile.SYMTYPE, content.target
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return zstandard.ZstdCompressor().compress(buffer.getvalue())


def paths_json(payload: dict) -> bytes:
    entries = [{"_path": name, "path_type": "softlink" if isinstance(content, Symlink) else "hardlink"}
               for name, content in payload.items() if content is not None]
    return json.dumps({"paths": entries, "paths_version": 1}).encode()


PAYLOAD = {"lib/R/library/DESeq2": None, "lib/R/library/DESeq2/DESCRIPTION": b"Package: DESeq2\n"}


def conda_package(directory: Path, index: dict, payload: dict = PAYLOAD, info: dict | None = None) -> Path:
    """Write a .conda file with ``index`` as info/index.json and ``payload`` as its files.

    info/paths.json lists the payload. ``info`` adds, replaces or, with None, removes files in info/.
    """
    stem = f"{index['name']}-{index['version']}-{index['build']}"
    path = Path(directory) / f"{stem}.conda"
    info_files = {"info/about.json": b"{}", "info/index.json": json.dumps(index).encode(),
                  "info/paths.json": paths_json(payload)} | (info or {})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("metadata.json", json.dumps({"conda_pkg_format_version": 2}))
        archive.writestr(f"pkg-{stem}.tar.zst", zst_tar(payload))
        archive.writestr(f"info-{stem}.tar.zst", zst_tar({k: v for k, v in info_files.items() if v is not None}))
    return path


class FakeRegistry:
    """In-memory stand-in for registry.Registry."""

    host = "ghcr.io"
    repository = "example/channel"
    reference = "ghcr.io/example/channel"

    def __init__(self, manifests=None, blobs=None):
        self.manifests = dict(manifests or {})
        self.blobs = dict(blobs or {})
        self.requested = []

    def list_tags(self):
        return sorted(self.manifests)

    def manifest(self, tag):
        self.requested.append(tag)
        return self.manifests.get(tag)

    def blob(self, digest):
        return self.blobs[digest]

    def open_blob(self, digest):
        return io.BytesIO(self.blobs[digest])
