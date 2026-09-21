"""Load and validate channel.toml."""

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import Error
from .naming import SUBDIRS

BIOC_REPOSITORIES = ("bioc", "data/annotation", "data/experiment", "workflows")
# Each target runs a reusable workflow with up to three jobs; this keeps a build run to a few hundred jobs.
MAX_SHARD_SIZE = 120

_VERSION = re.compile(r"\d+\.\d+")
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
_REPOSITORY = re.compile(r"[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)+")
_RUNNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*")
# Anaconda's own repository, which needs a paid license at organizations over 200 people.
_ANACONDA = re.compile(r"(?i)^(defaults|anaconda|main|r|msys2|pkgs/\w+)$|repo\.anaconda\.|conda\.anaconda\.org/(anaconda|main|r|msys2)(/|$)")


@dataclass(frozen=True)
class Channel:
    url: str
    upstream: tuple[str, ...]
    registry: str | None
    bioc_version: str
    repositories: tuple[str, ...]
    r_version: str
    platforms: dict[str, str]
    shard_size: int
    max_attempts: int
    pinning: str
    rattler_build_version: str
    rattler_build_sha256: dict[str, str]

    @property
    def build_subdirs(self) -> tuple[str, ...]:
        """Configured subdirs for compiled packages, in canonical order."""
        return tuple(s for s in SUBDIRS if s != "noarch" and s in self.platforms)

    @property
    def subdirs(self) -> tuple[str, ...]:
        return ("noarch", *self.build_subdirs)


def _value(table: dict, key: str, kind: type, where: str, required: bool = True):
    value = table.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise Error(f"{where}.{key} must be a {kind.__name__}")
    return value


def _strings(table: dict, key: str, where: str) -> tuple[str, ...]:
    value = _value(table, key, list, where)
    if not value or not all(isinstance(item, str) for item in value):
        raise Error(f"{where}.{key} must be a non-empty list of strings")
    return tuple(value)


def _match(pattern: re.Pattern, value: str, what: str) -> str:
    if not pattern.fullmatch(value):
        raise Error(f"invalid {what}: {value!r}")
    return value


def load_channel(path: Path) -> Channel:
    try:
        data = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise Error(f"cannot read {path}: {err}") from None

    channel = _value(data, "channel", dict, str(path))
    bioc = _value(data, "bioconductor", dict, str(path))
    r = _value(data, "r", dict, str(path))
    platforms = _value(data, "platforms", dict, str(path))
    build = _value(data, "build", dict, str(path))
    rattler = _value(build, "rattler-build", dict, "build")

    url = _value(channel, "url", str, "channel").rstrip("/")
    if not (url.startswith("https://") and _HOST.fullmatch(url.removeprefix("https://"))):
        raise Error(f"channel.url must be https://<host> in lowercase, without a port or path: {url!r}")
    upstream = tuple(_match(_CHANNEL, c, "upstream channel") for c in _strings(channel, "upstream", "channel"))
    for name in upstream:
        if _ANACONDA.search(name):
            raise Error(f"upstream channel {name!r} is Anaconda's repository, which needs a paid license; "
                        "use community channels such as conda-forge")
    registry = _value(channel, "registry", str, "channel", required=False)
    if registry is not None:
        _split_registry(registry)

    repositories = _strings(bioc, "repositories", "bioconductor")
    unknown = sorted(set(repositories) - set(BIOC_REPOSITORIES))
    if unknown:
        raise Error(f"unknown Bioconductor repositories: {', '.join(unknown)}")

    for subdir, runner in platforms.items():
        if subdir not in SUBDIRS:
            raise Error(f"unknown platform {subdir!r}; expected one of {', '.join(SUBDIRS)}")
        if not isinstance(runner, str) or not _RUNNER.fullmatch(runner):
            raise Error(f"invalid runner for {subdir}: {runner!r}")
    if "noarch" not in platforms:
        raise Error("platforms.noarch is required")
    if len(platforms) < 2:
        raise Error("platforms needs at least one subdir besides noarch")
    if platforms["noarch"] not in [runner for subdir, runner in platforms.items() if subdir != "noarch"]:
        raise Error("platforms.noarch must be the runner of another configured platform")

    shard_size = _value(build, "shard_size", int, "build")
    if not 1 <= shard_size <= MAX_SHARD_SIZE:
        raise Error(f"build.shard_size must be between 1 and {MAX_SHARD_SIZE}")
    max_attempts = _value(build, "max_attempts", int, "build")
    if max_attempts < 1:
        raise Error("build.max_attempts must be at least 1")

    sha256 = _value(rattler, "sha256", dict, "build.rattler-build")
    for target, digest in sha256.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise Error(f"invalid sha256 for rattler-build {target}")

    return Channel(
        url=url,
        upstream=upstream,
        registry=registry,
        bioc_version=_match(_VERSION, _value(bioc, "version", str, "bioconductor"), "Bioconductor version"),
        repositories=repositories,
        r_version=_match(_VERSION, _value(r, "version", str, "r"), "R version"),
        platforms=dict(platforms),
        shard_size=shard_size,
        max_attempts=max_attempts,
        pinning=_match(_COMMIT, _value(build, "pinning", str, "build"), "pinning commit"),
        rattler_build_version=_match(_SEMVER, _value(rattler, "version", str, "build.rattler-build"),
                                     "rattler-build version"),
        rattler_build_sha256=dict(sha256),
    )


def _split_registry(reference: str) -> tuple[str, str]:
    host, _, repository = reference.partition("/")
    if not _HOST.fullmatch(host) or not _REPOSITORY.fullmatch(repository):
        raise Error(f"invalid registry {reference!r}; expected <host>/<owner>/<name> in lowercase")
    return host, repository


def registry_reference(channel: Channel, environ=os.environ) -> tuple[str, str]:
    """Return (host, repository) of the registry holding the channel."""
    if channel.registry:
        return _split_registry(channel.registry)
    repository = environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise Error("set channel.registry in channel.toml, or GITHUB_REPOSITORY in the environment")
    # GHCR accepts lowercase names only.
    return _split_registry(f"ghcr.io/{repository.lower()}")
