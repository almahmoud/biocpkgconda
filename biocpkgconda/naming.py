"""Conda names, versions and match specs for R packages, and registry tags."""

import hashlib
import re
from dataclasses import dataclass

from . import Error

SUBDIRS = ("noarch", "linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64")

BASE_R = frozenset({
    "R", "base", "compiler", "datasets", "graphics", "grDevices", "grid", "methods",
    "parallel", "splines", "stats", "stats4", "tcltk", "tools", "utils",
})

_DEPENDENCY = re.compile(r"([A-Za-z][A-Za-z0-9.]*)\s*(?:\(\s*(>=|<=|==|>|<)\s*(\d+(?:[.-]\d+)*)\s*\))?")
_FILENAME = re.compile(r"[A-Za-z0-9._+!-]{1,255}")
_EXTENSIONS = (".conda", ".tar.bz2")
_TARGET = re.compile("(" + "|".join(map(re.escape, SUBDIRS)) + r")/([A-Za-z][A-Za-z0-9.]*)")


def bioc_name(package: str) -> str:
    return f"bioconductor-{package.lower()}"


def cran_name(package: str) -> str:
    return f"r-{package.lower()}"


def conda_version(version: str) -> str:
    return version.replace("-", "_")


@dataclass(frozen=True)
class Dependency:
    name: str
    op: str | None = None
    version: str | None = None

    @property
    def constraint(self) -> str:
        return f"{self.op}{conda_version(self.version)}" if self.op else ""


def parse_dependencies(field: str) -> list[Dependency]:
    """Parse an R Depends, Imports or LinkingTo field."""
    dependencies = []
    for item in field.split(","):
        item = item.strip()
        if not item:
            continue
        match = _DEPENDENCY.fullmatch(item)
        if not match:
            raise ValueError(f"cannot parse dependency {item!r}")
        dependencies.append(Dependency(*match.groups()))
    return dependencies


def match_spec(name: str, dependency: Dependency) -> str:
    return f"{name} {dependency.constraint}".rstrip()


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.split(r"[.\-_]", version) if part.isdigit())


def exceeds_r(required: str, r_version: str) -> bool:
    """True when an ``R (>= required)`` dependency needs a newer major.minor R."""
    return version_key(required)[:2] > version_key(r_version)[:2]


def r_base_pin(r_version: str) -> str:
    """The run requirement conda-forge R packages carry, e.g. ``r-base >=4.5,<4.6.0a0``."""
    major, minor = version_key(r_version)[:2]
    return f"r-base >={major}.{minor},<{major}.{minor + 1}.0a0"


def parse_target(target: str) -> tuple[str, str]:
    """Split ``<subdir>/<Package>`` into its parts."""
    match = _TARGET.fullmatch(target)
    if not match:
        raise Error(f"invalid target {target!r}; expected <subdir>/<Package>, e.g. linux-64/DESeq2")
    return match.group(1), match.group(2)


def split_filename(filename: str) -> tuple[str, str, str] | None:
    """Return (name, version, build) for a valid package filename, else None."""
    if not _FILENAME.fullmatch(filename):
        return None
    for extension in _EXTENSIONS:
        if filename.endswith(extension):
            parts = filename[: -len(extension)].rsplit("-", 2)
            if len(parts) == 3 and all(parts):
                return parts[0], parts[1], parts[2]
    return None


# Registry tags; biocpkgconda-proxy computes the same tags.
MAX_TAG = 128


def _escape(value: str) -> str:
    return value.replace("+", "__p__").replace("!", "__e__").replace("=", "__eq__")


def _fit(tag: str, key: str) -> str:
    if len(tag) <= MAX_TAG:
        return tag
    return tag[:111] + "_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def package_tag(subdir: str, filename: str) -> str:
    return _fit(f"{subdir}_{_escape(filename)}", f"{subdir}/{filename}")


def repodata_tag(subdir: str) -> str:
    return f"{subdir}_repodata"


def failure_tag(subdir: str, name: str, version: str) -> str:
    return _fit(f"failed_{subdir}_{_escape(name)}-{_escape(version)}", f"failed/{subdir}/{name}-{version}")
