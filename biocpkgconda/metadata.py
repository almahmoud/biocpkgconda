"""Package metadata from Bioconductor, CRAN and conda-forge."""

import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import Error, dcf
from .config import Channel
from .naming import BASE_R, Dependency, bioc_name, conda_version, cran_name, parse_dependencies
from .net import fetch

BIOCONDUCTOR = "https://bioconductor.org"
CRAN = "https://cloud.r-project.org"
CRAN_HOMEPAGE = "https://cran.r-project.org"
CONDA_FORGE = "https://conda.anaconda.org/conda-forge"

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9.]*")
_VERSION = re.compile(r"\d+([.-]\d+)+")
_MD5 = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    repository: str  # Bioconductor repository, or "cran"
    depends: tuple[Dependency, ...]  # Depends and Imports, without R and base packages
    linking_to: tuple[Dependency, ...]
    r_version: str | None  # from "R (>= x)" in Depends
    md5: str
    needs_compilation: bool
    license: str
    url: str
    homepage: str

    @property
    def is_bioc(self) -> bool:
        return self.repository != "cran"

    @property
    def conda_name(self) -> str:
        return bioc_name(self.name) if self.is_bioc else cran_name(self.name)

    @property
    def conda_version(self) -> str:
        return conda_version(self.version)

    @property
    def strong_dependencies(self) -> tuple[str, ...]:
        """Names from Depends, Imports and LinkingTo, in order and without duplicates."""
        return tuple(dict.fromkeys(d.name for d in (*self.depends, *self.linking_to)))


@dataclass(frozen=True)
class Sources:
    bioc: dict[str, Package]
    cran: dict[str, Package]

    def get(self, name: str) -> Package | None:
        return self.bioc.get(name) or self.cran.get(name)


def _skip(name: str, reason: str) -> None:
    print(f"warning: skipping PACKAGES record {name!r}: {reason}", file=sys.stderr)


def _package(record: dict, repository: str, url: str, homepage: str) -> Package | None:
    name = record["Package"]
    try:
        depends = parse_dependencies(record.get("Depends", "")) + parse_dependencies(record.get("Imports", ""))
        linking_to = parse_dependencies(record.get("LinkingTo", ""))
    except ValueError as err:
        _skip(name, str(err))
        return None
    r_version = next((d.version for d in depends if d.name == "R" and d.op in (">=", ">")), None)
    return Package(
        name=name,
        version=record["Version"],
        repository=repository,
        depends=tuple(d for d in depends if d.name not in BASE_R),
        linking_to=tuple(d for d in linking_to if d.name not in BASE_R),
        r_version=r_version,
        md5=md5 if _MD5.fullmatch(md5 := record.get("MD5sum", "").lower()) else "",
        needs_compilation=record.get("NeedsCompilation", "no").strip().lower() == "yes",
        license=" ".join(record.get("License", "").split()),
        url=url,
        homepage=homepage,
    )


def _records(text: str):
    """PACKAGES records with a valid package name and version."""
    for record in dcf.parse(text):
        name, version = record.get("Package", ""), record.get("Version", "")
        if _NAME.fullmatch(name) and _VERSION.fullmatch(version):
            yield record
        else:
            _skip(name, f"invalid name or version {version!r}")


def parse_bioc(text: str, bioc_version: str, repository: str) -> dict[str, Package]:
    base = f"{BIOCONDUCTOR}/packages/{bioc_version}/{repository}"
    packages = {}
    for record in _records(text):
        name, version = record["Package"], record["Version"]
        package = _package(record, repository, f"{base}/src/contrib/{name}_{version}.tar.gz",
                           f"{base}/html/{name}.html")
        if package:
            packages[name] = package
    return packages


def parse_cran(text: str) -> dict[str, Package]:
    packages = {}
    for record in _records(text):
        # Records with a Path field are copies built for other R versions.
        if "Path" in record:
            continue
        name, version = record["Package"], record["Version"]
        package = _package(record, "cran", f"{CRAN}/src/contrib/{name}_{version}.tar.gz",
                           f"{CRAN_HOMEPAGE}/package={name}")
        if package:
            packages[name] = package
    return packages


def bioc_packages(channel: Channel, cache: Path | None = None) -> dict[str, Package]:
    packages = {}
    for repository in channel.repositories:
        url = f"{BIOCONDUCTOR}/packages/{channel.bioc_version}/{repository}/src/contrib/PACKAGES"
        packages.update(parse_bioc(fetch(url, cache).decode("utf-8", "replace"), channel.bioc_version, repository))
    return packages


def cran_packages(cache: Path | None = None) -> dict[str, Package]:
    return parse_cran(fetch(f"{CRAN}/src/contrib/PACKAGES", cache).decode("utf-8", "replace"))


def r_version_for_bioc(config_yaml: str, bioc_version: str) -> str | None:
    """Read ``r_ver_for_bioc_ver`` from Bioconductor's config.yaml."""
    section = re.search(r"^r_ver_for_bioc_ver:\n((?:[ \t]+.*\n?)*)", config_yaml, re.MULTILINE)
    if not section:
        return None
    for key, value in re.findall(r"""^\s+["']?([\d.]+)["']?\s*:\s*["']?([\d.]+)["']?""", section.group(1),
                                 re.MULTILINE):
        if key == bioc_version:
            return value
    return None


def check_r_version(channel: Channel, cache: Path | None = None) -> None:
    text = fetch(f"{BIOCONDUCTOR}/config.yaml", cache).decode("utf-8", "replace")
    expected = r_version_for_bioc(text, channel.bioc_version)
    if expected is None:
        raise Error(f"Bioconductor {channel.bioc_version} is not listed in {BIOCONDUCTOR}/config.yaml")
    if expected != channel.r_version:
        raise Error(f"Bioconductor {channel.bioc_version} requires R {expected}, "
                    f"but channel.toml sets r.version = {channel.r_version!r}")


def load(channel: Channel, cache: Path | None = None) -> Sources:
    check_r_version(channel, cache)
    return Sources(bioc_packages(channel, cache), cran_packages(cache))


def r_packages_for(repodata: dict, r_version: str) -> set[str]:
    """Names of r-* packages with a build pinned to ``r_version``."""
    pin = re.compile(rf"r-base >={re.escape(r_version)},")
    names = set()
    for key in ("packages", "packages.conda"):
        for record in repodata.get(key, {}).values():
            name = record.get("name", "")
            if name.startswith("r-") and name not in names and any(pin.match(d) for d in record.get("depends", ())):
                names.add(name)
    return names


def conda_forge_r(channel: Channel, cache: Path | None = None) -> frozenset[str]:
    """r-* packages with a conda-forge build for the channel's R in noarch or in every configured platform subdir."""
    # Imported here so the rest of the package works without zstandard installed.
    import zstandard

    def names(subdir: str) -> set[str]:
        data = fetch(f"{CONDA_FORGE}/{subdir}/repodata.json.zst", cache)
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True) as stream:
            return r_packages_for(json.load(stream), channel.r_version)

    return frozenset(names("noarch").union(set.intersection(*map(names, channel.build_subdirs))))
