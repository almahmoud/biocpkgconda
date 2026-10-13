"""Load and validate channel.toml, overrides.toml and sysreqs.toml."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import Error
from .naming import R_PACKAGE, SUBDIRS

BIOC_REPOSITORIES = ("bioc", "data/annotation", "data/experiment", "workflows")
# Each target runs a reusable workflow with up to three jobs; this keeps a build run to a few hundred jobs.
MAX_SHARD_SIZE = 120

_VERSION = re.compile(r"\d+\.\d+")
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
_REPOSITORY = re.compile(r"[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)+")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_RUNNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*")
# Anaconda's own repository, which needs a paid license at organizations over 200 people.
_ANACONDA = re.compile(r"(?i)^(defaults|anaconda|main|r|msys2|pkgs/\w+)$|repo\.anaconda\.|conda\.anaconda\.org/(anaconda|main|r|msys2)(/|$)")
_MATCH_SPEC = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]*( [A-Za-z0-9.*_,<>=!|+-]+){0,2}")
_MAKEVARS_LINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]* *[+:?]?= *[^\x00-\x1f\x7f]*")
_PATCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.patch")
_GLOB = re.compile(r"[A-Za-z0-9._/*?{},-]+")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_ENV_VALUE = re.compile(r"([A-Za-z0-9 ,._/=+:@-]|\$\{\{ (PREFIX|BUILD_PREFIX) \}\})*")
_RESERVED_ENV = frozenset({"PATH", "PREFIX", "BUILD_PREFIX", "SRC_DIR", "HOME"})


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
    image: str | None
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


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise Error(f"cannot read {path}: {err}") from None


def load_channel(path: Path) -> Channel:
    data = _read(path)
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
    image = _value(build, "image", str, "build", required=False)
    if image is not None:
        _check_image(image)

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
        image=image,
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


def _check_image(reference: str) -> str:
    name, _, digest = reference.partition("@")
    host, _, path = name.partition("/")
    repository, _, tag = path.partition(":")
    if not (_HOST.fullmatch(host) and _REPOSITORY.fullmatch(repository) and (tag or digest)
            and (not tag or _TAG.fullmatch(tag)) and (not digest or _DIGEST.fullmatch(digest))):
        raise Error(f"invalid image {reference!r}; expected <host>/<owner>/<name>:<tag> or "
                    "<host>/<owner>/<name>@sha256:<digest> in lowercase")
    return reference


def builder_image(channel: Channel, environ=os.environ) -> str:
    """Container image that Linux packages are built in."""
    if channel.image:
        return channel.image
    repository = environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise Error("set build.image in channel.toml, or GITHUB_REPOSITORY in the environment")
    owner = repository.partition("/")[0].lower()
    return _check_image(f"ghcr.io/{owner}/biocpkgconda-builder:{channel.bioc_version}")


@dataclass(frozen=True)
class Rule:
    """A sysreqs.toml rule."""

    pattern: re.Pattern
    build: tuple[str, ...] = ()
    host: tuple[str, ...] = ()
    run: tuple[str, ...] = ()
    platforms: tuple[str, ...] | None = None

    def applies(self, system_requirements: str, subdir: str) -> bool:
        if self.platforms is not None and subdir not in self.platforms:
            return False
        return bool(self.pattern.search(system_requirements))


@dataclass(frozen=True)
class Override:
    """An overrides.toml entry."""

    build: tuple[str, ...] = ()
    host: tuple[str, ...] = ()
    run: tuple[str, ...] = ()
    skip: str | None = None
    makevars: dict[str, tuple[str, ...]] = field(default_factory=dict)  # by subdir
    patches: tuple[Path, ...] = ()
    binary_relocation: dict[str, bool | tuple[str, ...]] = field(default_factory=dict)  # by subdir: False or globs
    env: dict[str, str] = field(default_factory=dict)


def _specs(table: dict, key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and _MATCH_SPEC.fullmatch(v) for v in value):
        raise Error(f"{where}.{key} must be a list of conda match specs such as \"gsl\" or \"libxml2 >=2.12\"")
    return tuple(value)


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise Error(f"{where}: unknown keys {', '.join(unknown)}")


def load_sysreqs(path: Path) -> tuple[Rule, ...]:
    data = _read(path)
    _check_keys(data, {"rule"}, Path(path).name)
    rules = []
    for number, table in enumerate(data.get("rule", []), 1):
        where = f"{Path(path).name} rule {number}"
        _check_keys(table, {"pattern", "build", "host", "run", "platforms"}, where)
        try:
            pattern = re.compile(_value(table, "pattern", str, where))
        except re.error as err:
            raise Error(f"{where}: invalid pattern: {err}") from None
        platforms = table.get("platforms")
        if platforms is not None:
            platforms = _strings(table, "platforms", where)
            unknown = sorted(set(platforms) - set(SUBDIRS))
            if unknown:
                raise Error(f"{where}: unknown platforms {', '.join(unknown)}")
        rules.append(Rule(pattern, _specs(table, "build", where), _specs(table, "host", where),
                          _specs(table, "run", where), platforms))
    return tuple(rules)


def _by_subdir(table: dict, key: str, where: str) -> dict:
    value = table.get(key, {})
    if not isinstance(value, dict):
        raise Error(f"{where}.{key} must be a table keyed by subdir")
    for subdir in value:
        if subdir == "noarch" or subdir not in SUBDIRS:
            raise Error(f"{where}.{key}: {subdir!r} is not a platform subdir such as linux-64")
    return value


def _makevars(table: dict, where: str) -> dict[str, tuple[str, ...]]:
    makevars = {}
    for subdir, lines in _by_subdir(table, "makevars", where).items():
        if not (isinstance(lines, list) and lines
                and all(isinstance(line, str) and _MAKEVARS_LINE.fullmatch(line) for line in lines)):
            raise Error(f"{where}.makevars.{subdir} must be a non-empty list of make assignments "
                        "such as \"CFLAGS += -O2\"")
        makevars[subdir] = tuple(lines)
    return makevars


def _patches(table: dict, where: str, directory: Path) -> tuple[Path, ...]:
    names = table.get("patches", [])
    if (not isinstance(names, list) or not all(isinstance(n, str) and _PATCH.fullmatch(n) for n in names)
            or len(set(names)) != len(names)):
        raise Error(f"{where}.patches must be a list of distinct file names ending in .patch")
    paths = tuple(directory / name for name in names)
    for path in paths:
        if any(p.is_symlink() for p in (directory.parent, directory, path)) or not path.is_file():
            raise Error(f"{where}.patches: {path} is not a file")
    return paths


def _binary_relocation(table: dict, where: str) -> dict[str, bool | tuple[str, ...]]:
    relocation = {}
    for subdir, value in _by_subdir(table, "binary_relocation", where).items():
        # true is rattler-build's default, and an empty list relocates nothing, which false says directly.
        if value is False:
            relocation[subdir] = False
        elif isinstance(value, list) and value and all(isinstance(g, str) and _GLOB.fullmatch(g) for g in value):
            relocation[subdir] = tuple(value)
        else:
            raise Error(f"{where}.binary_relocation.{subdir} must be false or a non-empty list of globs "
                        "relative to the prefix")
    return relocation


def _env(table: dict, where: str) -> dict[str, str]:
    env = table.get("env", {})
    if not isinstance(env, dict):
        raise Error(f"{where}.env must be a table")
    for name, value in env.items():
        if not _ENV_NAME.fullmatch(name) or name in _RESERVED_ENV:
            raise Error(f"{where}.env: {name!r} is not an allowed variable name")
        if not isinstance(value, str) or not _ENV_VALUE.fullmatch(value):
            raise Error(f"{where}.env.{name} may hold only letters, digits, spaces, the characters ,._/=+:@- "
                        "and ${{ PREFIX }} or ${{ BUILD_PREFIX }}")
    return dict(env)


def load_overrides(path: Path) -> dict[str, Override]:
    data = _read(path)
    _check_keys(data, {"package"}, Path(path).name)
    patches = Path(path).parent / "patches"
    overrides = {}
    for name, table in data.get("package", {}).items():
        where = f"package.{name}"
        if not R_PACKAGE.fullmatch(name):
            raise Error(f"{where}: invalid R package name")
        if not isinstance(table, dict):
            raise Error(f"{where} must be a table")
        _check_keys(table, {"build", "host", "run", "skip", "makevars", "patches", "binary_relocation", "env"},
                    where)
        skip = _value(table, "skip", str, where, required=False)
        if skip is not None and not skip.strip():
            raise Error(f"{where}.skip must give a reason")
        overrides[name] = Override(
            build=_specs(table, "build", where),
            host=_specs(table, "host", where),
            run=_specs(table, "run", where),
            skip=skip,
            makevars=_makevars(table, where),
            patches=_patches(table, where, patches / name),
            binary_relocation=_binary_relocation(table, where),
            env=_env(table, where),
        )
    return overrides


# Shown on the registry package page.
REGISTRY_DESCRIPTION = "Packages and repodata of the Bioconductor conda channel"


def source_url(repository: str, environ=os.environ) -> str:
    """URL of the repository that builds the channel, for org.opencontainers.image.source."""
    server = environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return f"{server}/{environ.get('GITHUB_REPOSITORY') or repository}"
