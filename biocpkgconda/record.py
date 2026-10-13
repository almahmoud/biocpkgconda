"""Push conda packages and failure markers to the registry with oras."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unicodedata
import zipfile
from pathlib import Path

from . import Error
from .config import REGISTRY_DESCRIPTION
from .metadata import Package, Sources
from .naming import failure_tag, package_tag, split_filename
from .registry import FAILURE, INDEX, PACKAGE_V2, Registry

ATTEMPTS = "org.biocpkgconda.attempts"
STAGE = "org.biocpkgconda.stage"
# Steps of package.yml whose failure counts as a failed build.
STAGES = ("recipe", "build", "package", "verify", "check")
LIBRARY = "lib/R/library"
MAX_INDEX_SIZE = 64 * 1024
MAX_PATHS_SIZE = 64 * 1024 * 1024
_RUN_URL = re.compile(r"https://[^\s\"']{1,500}")
_INFO_SIZES = {"info/index.json": MAX_INDEX_SIZE, "info/paths.json": MAX_PATHS_SIZE}
# conda reads Python entry points from these files whatever the noarch type in info/index.json.
_ENTRY_POINT_FILES = ("info/link.json", "info/package_metadata.json")
_PATH_TYPES = ("hardlink", "softlink", "directory")


def parse_json(raw: bytes):
    """Decoded JSON, or None when ``raw`` is not valid JSON."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _shown(value) -> str:
    """A value read from package metadata, for an error message."""
    return repr(value[:200]) if isinstance(value, str) else "a value that is not a string"


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_specs(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_text(value) -> bool:
    return value is None or isinstance(value, str)


# Fields copied from info/index.json into repodata, with their checks. Other fields are dropped.
_INDEX_FIELDS = {
    "build_number": _is_count,
    "depends": _is_specs,
    "constrains": _is_specs,
    "license": _is_text,
    "license_family": _is_text,
    "arch": _is_text,
    "platform": _is_text,
    "noarch": lambda value: value == "generic",
    "timestamp": _is_count,
}
_REQUIRED_FIELDS = ("build_number", "depends")


def index_record(index, filename: str, subdir: str) -> dict:
    """The repodata record for ``subdir/filename`` from its info/index.json."""
    if not isinstance(index, dict):
        raise Error(f"{filename}: info/index.json is not a JSON object")
    if index.get("subdir") != subdir:
        raise Error(f"{filename} is built for {_shown(index.get('subdir'))}, not {subdir!r}")
    if (index.get("name"), index.get("version"), index.get("build")) != split_filename(filename):
        raise Error(f"{filename} does not match the name, version and build in its info/index.json")
    record = {key: index[key] for key in ("name", "version", "build", "subdir")}
    for key, valid in _INDEX_FIELDS.items():
        if key not in index:
            if key in _REQUIRED_FIELDS:
                raise Error(f"{filename}: info/index.json has no {key}")
        elif not valid(index[key]):
            raise Error(f"{filename}: info/index.json has an invalid {key}")
        else:
            record[key] = index[key]
    return record


def _is_normal(path: str) -> bool:
    """Whether a relative path is printable, separated by ``/`` only, and has no empty, ``.`` or ``..`` segments."""
    return path.isprintable() and "\\" not in path and all(part not in ("", ".", "..") for part in path.split("/"))


def _is_local(target: str) -> bool:
    """Whether a symbolic link target stays below the directory holding the link."""
    return (bool(target) and target.isprintable() and "\\" not in target and not target.startswith("/")
            and ".." not in target.split("/"))


def package_paths(raw: bytes | None, filename: str, package: str) -> dict[str, str]:
    """Path type of each entry in info/paths.json, after checking all are in the R library directory of ``package``."""
    if raw is None:
        raise Error(f"{filename}: info/paths.json not found")
    data = parse_json(raw)
    entries = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise Error(f"{filename}: info/paths.json has no list of paths")
    prefix = f"{LIBRARY}/{package}/"
    paths: dict[str, str] = {}
    for entry in entries:
        path = entry.get("_path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path.startswith(prefix) or not _is_normal(path):
            raise Error(f"{filename} installs {_shown(path)}, outside {prefix}")
        kind = entry.get("path_type")
        if kind not in _PATH_TYPES:
            raise Error(f"{filename}: {path} has path type {_shown(kind)}")
        if path in paths:
            raise Error(f"{filename}: {path} is listed twice in info/paths.json")
        paths[path] = kind
    return paths


def _check_layout(path: Path) -> None:
    """Check the zip archive holds only the members of a .conda file."""
    # rattler unpacks every .tar.zst member, not only the info and pkg archives.
    stem = path.name.removesuffix(".conda")
    expected = [f"info-{stem}.tar.zst", "metadata.json", f"pkg-{stem}.tar.zst"]
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
    except (zipfile.BadZipFile, OSError) as err:
        raise Error(f"{path.name}: cannot read package: {err}") from None
    if names != expected:
        raise Error(f"{path.name}: the archive must hold exactly {', '.join(expected)}")


def _scan(path: Path, component: str, visit) -> None:
    """Call ``visit(tar, member)`` for each member of the ``info`` or ``pkg`` archive of a .conda file."""
    # Imported here so the rest of the package works without zstandard installed.
    import zstandard

    name = f"{component}-{path.name.removesuffix('.conda')}.tar.zst"
    try:
        with zipfile.ZipFile(path) as archive, archive.open(name) as raw, \
                zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True) as stream, \
                tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                visit(tar, member)
    except (KeyError, zipfile.BadZipFile, tarfile.TarError, zstandard.ZstdError, OSError) as err:
        raise Error(f"{path.name}: cannot read package: {err}") from None


def read_info(path: Path) -> dict[str, bytes]:
    """info/index.json and info/paths.json of a .conda file, where present."""
    _check_layout(path)
    files: dict[str, bytes] = {}
    # Unpacking can map other spellings of a name onto the same file, e.g. on case-insensitive file systems.
    guarded = {name.casefold(): name for name in (*_INFO_SIZES, *_ENTRY_POINT_FILES)}

    def visit(tar, member):
        name = member.name
        if not (name == "info" or name.startswith("info/")) or not _is_normal(name):
            raise Error(f"{path.name}: unexpected entry {name!r} in the info archive")
        canonical = guarded.get(unicodedata.normalize("NFKC", name).casefold())
        if canonical is None:
            return
        if canonical in _ENTRY_POINT_FILES:
            raise Error(f"{path.name} has {name}; conda creates entry points from it")
        if name != canonical or name in files or not member.isfile():
            raise Error(f"{path.name}: {name!r} in the info archive clashes with {canonical}")
        if member.size > _INFO_SIZES[name]:
            raise Error(f"{path.name}: {name} is larger than {_INFO_SIZES[name]} bytes")
        files[name] = tar.extractfile(member).read()

    _scan(path, "info", visit)
    return files


def check_payload(path: Path, paths: dict[str, str], package: str) -> None:
    """Check the files of a .conda file are exactly those in its info/paths.json, and links stay local."""
    root = f"{LIBRARY}/{package}"
    parts = root.split("/")
    parents = {"/".join(parts[:end]) for end in range(1, len(parts) + 1)}
    seen: set[str] = set()

    def visit(tar, member):
        name = member.name
        if name in seen:
            raise Error(f"{path.name}: {name!r} appears twice in the package")
        seen.add(name)
        kind = paths.get(name)
        if member.issym():
            if kind == "softlink" and not _is_local(member.linkname):
                raise Error(f"{path.name}: {name} links to {member.linkname!r}, outside its directory")
            matches = kind == "softlink"
        elif member.isdir():
            matches = name in parents or (name.startswith(root + "/") and _is_normal(name))
        else:
            matches = member.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and kind == "hardlink"
        if not matches:
            raise Error(f"{path.name}: {name!r} in the package does not match info/paths.json")

    _scan(path, "pkg", visit)
    missing = sorted(name for name, kind in paths.items() if kind != "directory" and name not in seen)
    if missing:
        raise Error(f"{path.name}: {missing[0]} is in info/paths.json but not in the package")


def digests(path: Path) -> tuple[str, str, int]:
    """md5, sha256 and size of a file."""
    md5, sha256 = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest(), path.stat().st_size


def oras_push(reference: str, files: list[tuple[str, str]], annotations: dict, workdir: Path,
              run=subprocess.run) -> None:
    """Push files from ``workdir`` so each layer title is the bare file name."""
    (workdir / "annotations.json").write_text(json.dumps(annotations, indent=2, sort_keys=True))
    command = ["oras", "push", "--image-spec", "v1.0", reference,
               *(f"{name}:{media_type}" for name, media_type in files),
               "--annotation-file", "annotations.json"]
    try:
        run(command, cwd=workdir, check=True)
    except FileNotFoundError:
        raise Error("oras is not installed or not on PATH") from None
    except subprocess.CalledProcessError as err:
        raise Error(f"oras push {reference} failed with exit code {err.returncode}") from None


def _stage(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _package(sources: Sources, name: str) -> Package:
    package = sources.get(name)
    if package is None:
        raise Error(f"{name} is not in Bioconductor or CRAN")
    return package


def _check_package(path: Path, subdir: str, package: Package) -> dict:
    """The repodata record of a built file, after checking it is the expected package and installs only its files."""
    if not path.is_file():
        raise Error(f"{path} does not exist")
    if split_filename(path.name) is None or not path.name.endswith(".conda"):
        raise Error(f"{path.name} is not a valid .conda package file name")
    info = read_info(path)
    if "info/index.json" not in info:
        raise Error(f"{path.name}: info/index.json not found")
    record = index_record(parse_json(info["info/index.json"]), path.name, subdir)
    if (record["name"], record["version"]) != (package.conda_name, package.conda_version):
        raise Error(f"{path.name} is not a build of {package.conda_name} {package.conda_version}")
    check_payload(path, package_paths(info.get("info/paths.json"), path.name, package.name), package.name)
    return record


def check(paths: list[Path], target: tuple[str, str], sources: Sources) -> list[tuple[Path, dict]]:
    """Each built file of a target with its repodata record.

    Refuses files of any other package or version, and packages that install files outside
    ``lib/R/library/<Package>/``, including link and activation scripts.
    """
    subdir, name = target
    package = _package(sources, name)
    return [(Path(p), _check_package(Path(p), subdir, package)) for p in paths]


def publish(paths: list[Path], target: tuple[str, str], registry: Registry, sources: Sources, source: str,
            run=subprocess.run) -> None:
    """Push the built files of a target after the checks of :func:`check`.

    A file that is already published is left unchanged.
    """
    subdir = target[0]
    for path, record in check(paths, target, sources):
        tag = package_tag(subdir, path.name)
        if registry.manifest(tag) is not None:
            print(f"::notice::{subdir}/{path.name} is already published; left unchanged")
            continue
        md5, _, _ = digests(path)
        annotations = {
            path.name: {"org.conda.md5": md5},
            "$manifest": {
                "org.opencontainers.image.source": source,
                "org.opencontainers.image.description": REGISTRY_DESCRIPTION,
                "org.conda.subdir": subdir,
                "org.conda.filename": path.name,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            _stage(path, workdir / path.name)
            (workdir / "index.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            oras_push(f"{registry.reference}:{tag}", [(path.name, PACKAGE_V2), ("index.json", INDEX)],
                      annotations, workdir, run)
        print(f"published {subdir}/{path.name} to {registry.reference}:{tag}")


def attempts(manifest: dict | None) -> int:
    """Failed attempts recorded in a failure marker manifest."""
    value = ((manifest or {}).get("annotations") or {}).get(ATTEMPTS, "0")
    return int(value) if value.isdigit() else 0


def fail(target: tuple[str, str], run_url: str, stage: str, registry: Registry, sources: Sources, source: str,
         run=subprocess.run) -> int:
    """Record a build of the current version of a package that failed at ``stage``. Returns the attempt count."""
    subdir, name = target
    package = _package(sources, name)
    if not _RUN_URL.fullmatch(run_url):
        raise Error(f"invalid run URL {run_url!r}")
    if stage not in STAGES:
        raise Error(f"invalid stage {stage!r}; expected one of {', '.join(STAGES)}")
    tag = failure_tag(subdir, package.conda_name, package.conda_version)
    count = attempts(registry.manifest(tag)) + 1
    marker = {"name": package.conda_name, "version": package.conda_version, "subdir": subdir,
              "attempts": count, "run_url": run_url, "stage": stage}
    annotations = {"$manifest": {
        "org.opencontainers.image.source": source,
        "org.opencontainers.image.description": REGISTRY_DESCRIPTION,
        ATTEMPTS: str(count),
        "org.biocpkgconda.run-url": run_url,
        STAGE: stage,
        "org.conda.subdir": subdir,
        "org.conda.name": package.conda_name,
        "org.conda.version": package.conda_version,
    }}
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "failure.json").write_text(json.dumps(marker, indent=2) + "\n")
        oras_push(f"{registry.reference}:{tag}", [("failure.json", FAILURE)], annotations, workdir, run)
    print(f"recorded failed attempt {count} at {stage} for {subdir}/{package.conda_name}-{package.conda_version}")
    return count
