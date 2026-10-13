"""The status.json document: install and build status of every package on each platform, and build failures."""

import json
from datetime import datetime, timezone

from .plan import Planner

SCHEMA = 1


def _platform(planner: Planner, name: str, platform: str) -> dict:
    usable = planner.usable(name, platform)
    return {"install": usable.status, "build": planner.status(name, platform).status,
            "reason": usable.reason, "root": usable.root}


def document(planner: Planner, failures: list[dict], now: datetime | None = None) -> dict:
    """status.json for the units of ``planner``, with the failure markers of their current versions.

    ``failures`` are the markers of the channel state; ``now`` is the generation time in UTC.
    """
    channel = planner.channel
    markers: dict[tuple[str, str], list[dict]] = {}
    for marker in failures:
        if marker["subdir"] in channel.subdirs:
            markers.setdefault((marker["name"], marker["version"]), []).append(marker)
    packages, failed = {}, {}
    for name in sorted(planner.units):
        package = planner.units[name]
        packages[name] = {
            "version": package.version,
            "repo": package.repository,
            "noarch": not package.needs_compilation,
            "depends": [d for d in package.strong_dependencies if d in planner.units],
            "platforms": {platform: _platform(planner, name, platform) for platform in channel.build_subdirs},
        }
        current = markers.get((package.conda_name, package.conda_version))
        if current:
            failed[name] = [{"subdir": m["subdir"], "version": m["version"], "attempts": m["attempts"],
                             "run_url": m["run_url"], "stage": m.get("stage", "")}
                            for m in sorted(current, key=lambda m: m["subdir"])]
    return {
        "schema": SCHEMA,
        "generated": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channel": channel.url,
        "bioconductor": channel.bioc_version,
        "r": channel.r_version,
        "platforms": list(channel.build_subdirs),
        "packages": packages,
        "failures": failed,
    }


def serialize(document: dict) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))
