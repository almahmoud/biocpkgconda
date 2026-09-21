"""Rebuild per-subdir repodata from the packages in the registry."""

import concurrent.futures
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import Error
from .config import REGISTRY_DESCRIPTION
from .naming import failure_tag, package_tag, repodata_tag, split_filename
from .record import MAX_INDEX_SIZE, attempts, index_record, oras_push, parse_json
from .registry import INDEX, PACKAGE_V1, PACKAGE_V2, REPODATA, REPODATA_ZST, Registry, layer

WORKERS = 8
_MD5 = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def empty_repodata(subdir: str) -> dict:
    return {"info": {"subdir": subdir}, "packages": {}, "packages.conda": {}, "removed": [], "repodata_version": 1}


def serialize(repodata: dict) -> bytes:
    return json.dumps(repodata, sort_keys=True, separators=(",", ":")).encode()


def package_tags(tags: list[str], subdir: str) -> set[str]:
    return {tag for tag in tags if tag.startswith(f"{subdir}_") and tag != repodata_tag(subdir)}


def load_repodata(registry: Registry, subdir: str) -> tuple[dict, bytes | None]:
    """Current repodata of a subdir and its raw bytes, or empty repodata and None."""
    manifest = registry.manifest(repodata_tag(subdir))
    item = layer(manifest, REPODATA) if manifest else None
    if item is None:
        return empty_repodata(subdir), None
    raw = registry.blob(item["digest"])
    repodata = json.loads(raw)
    for key in ("packages", "packages.conda"):
        repodata.setdefault(key, {})
    return repodata, raw


def package_record(registry: Registry, subdir: str, tag: str) -> tuple[str, dict] | None:
    """Filename and repodata record for a package tag, or None when the manifest is unusable."""
    manifest = registry.manifest(tag)
    if manifest is None:
        return None
    package = layer(manifest, PACKAGE_V2, PACKAGE_V1)
    info = layer(manifest, INDEX)
    if package is None or info is None:
        _warn(f"{tag}: manifest has no package or index.json layer")
        return None
    annotations = package.get("annotations") or {}
    filename = annotations.get("org.opencontainers.image.title", "")
    if split_filename(filename) is None or package_tag(subdir, filename) != tag:
        _warn(f"{tag}: package layer title {filename!r} does not belong to this tag")
        return None
    md5 = annotations.get("org.conda.md5", "")
    size = package.get("size")
    if not _MD5.fullmatch(md5) or not _SHA256.fullmatch(str(package.get("digest"))) or not isinstance(size, int):
        _warn(f"{tag}: package layer lacks an md5 annotation, sha256 digest or size")
        return None
    if not isinstance(info.get("size"), int) or info["size"] > MAX_INDEX_SIZE:
        _warn(f"{tag}: index.json layer has no size or is larger than {MAX_INDEX_SIZE} bytes")
        return None
    raw = registry.blob(info["digest"])
    try:
        record = index_record(parse_json(raw), filename, subdir)
    except Error as err:
        _warn(f"{tag}: {err}")
        return None
    return filename, dict(record, md5=md5, sha256=package["digest"].removeprefix("sha256:"), size=size)


def _is_current(record, filename: str, subdir: str) -> bool:
    """Whether a repodata record holds exactly the fields ``package_record`` writes, with valid values."""
    try:
        fields = index_record(record, filename, subdir)
    except Error:
        return False
    md5, sha256, size = (record.get(key) for key in ("md5", "sha256", "size"))
    return (record == dict(fields, md5=md5, sha256=sha256, size=size) and isinstance(size, int)
            and bool(_MD5.fullmatch(str(md5))) and bool(_SHA256.fullmatch(f"sha256:{sha256}")))


def reconcile(registry: Registry, subdir: str, tags: list[str]) -> tuple[dict, bytes, bool, int]:
    """Repodata matching the package tags of a subdir, its serialization, whether it changed, and records added."""
    repodata, previous = load_repodata(registry, subdir)
    known = {*repodata["packages"], *repodata["packages.conda"]}
    present = package_tags(tags, subdir)
    covered = set()
    for key in ("packages", "packages.conda"):
        for filename, record in list(repodata[key].items()):
            tag = package_tag(subdir, filename)
            if tag in present and _is_current(record, filename, subdir):
                covered.add(tag)
            else:
                del repodata[key][filename]
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        for result in pool.map(lambda tag: package_record(registry, subdir, tag), sorted(present - covered)):
            if result:
                filename, record = result
                repodata["packages.conda" if filename.endswith(".conda") else "packages"][filename] = record
    added = len({*repodata["packages"], *repodata["packages.conda"]} - known)
    data = serialize(repodata)
    return repodata, data, data != previous, added


def push_repodata(registry: Registry, subdir: str, data: bytes, source: str, run=subprocess.run) -> None:
    # Imported here so the rest of the package works without zstandard installed.
    import zstandard

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "repodata.json").write_bytes(data)
        (workdir / "repodata.json.zst").write_bytes(zstandard.ZstdCompressor(level=16).compress(data))
        oras_push(f"{registry.reference}:{repodata_tag(subdir)}",
                  [("repodata.json", REPODATA), ("repodata.json.zst", REPODATA_ZST)],
                  {"$manifest": {"org.opencontainers.image.source": source,
                                 "org.opencontainers.image.description": REGISTRY_DESCRIPTION}}, workdir, run)


def _failure(registry: Registry, tag: str) -> dict | None:
    manifest = registry.manifest(tag)
    annotations = (manifest or {}).get("annotations") or {}
    entry = {key: annotations.get(f"org.conda.{key}", "") for key in ("subdir", "name", "version")}
    if not all(entry.values()) or failure_tag(entry["subdir"], entry["name"], entry["version"]) != tag:
        _warn(f"{tag}: failure marker without matching subdir, name and version annotations")
        return None
    return entry | {"attempts": attempts(manifest), "run_url": annotations.get("org.biocpkgconda.run-url", "")}


def failures(registry: Registry, tags: list[str]) -> list[dict]:
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        markers = pool.map(lambda tag: _failure(registry, tag), sorted(t for t in tags if t.startswith("failed_")))
        return [marker for marker in markers if marker]


def index(registry: Registry, subdirs: tuple[str, ...], source: str, run=subprocess.run) -> tuple[dict, int]:
    """Reconcile and push repodata for each subdir. Returns the channel state the planner reads and records added."""
    tags = registry.list_tags()
    state = {"subdirs": {}, "failures": failures(registry, tags)}
    total = 0
    for subdir in subdirs:
        repodata, data, changed, added = reconcile(registry, subdir, tags)
        records = [*repodata["packages"].values(), *repodata["packages.conda"].values()]
        if changed:
            push_repodata(registry, subdir, data, source, run)
        print(f"{subdir}: {len(records)} packages, {added} added, repodata {'pushed' if changed else 'unchanged'}")
        state["subdirs"][subdir] = [{"name": r["name"], "version": r["version"], "depends": r.get("depends", [])}
                                    for r in records]
        total += added
    return state, total
