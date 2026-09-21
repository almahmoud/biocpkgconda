import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path

from biocpkgconda import config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def channel():
    """channel.toml with the Bioconductor and R versions of the fixtures."""
    return replace(config.load_channel(ROOT / "channel.toml"), bioc_version="3.23", r_version="4.6")


requires_zstandard = unittest.skipUnless(importlib.util.find_spec("zstandard"),
                                         "zstandard is not installed (pip install -r requirements.txt)")
