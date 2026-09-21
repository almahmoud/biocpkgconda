import hashlib
import importlib.util
import io
import tarfile
import unittest
from dataclasses import replace
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
