"""GitHub Actions steps: channel and build checks, build matrix and build dispatch."""

import json
import os
import re
import subprocess
import time

from . import Error, net
from .config import MAX_SHARD_SIZE, Channel
from .naming import parse_target

ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
MAX_TARGETS = MAX_SHARD_SIZE
RETRY_DELAYS = (5, 10, 20, 40)
PAUSE = 2
sleep = time.sleep

_LABEL = re.compile(r"[A-Za-z0-9 ._/-]{1,64}")
_RUN_ID = re.compile(r"[0-9]*")


def _append(environ, variable: str, text: str) -> None:
    path = environ.get(variable)
    if path:
        with open(path, "a") as handle:
            handle.write(text)


def set_output(name: str, value: str, environ=os.environ) -> None:
    if environ.get("GITHUB_OUTPUT"):
        _append(environ, "GITHUB_OUTPUT", f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def check_channel(channel: Channel, environ=os.environ) -> bool:
    """Whether the channel URL serves repodata.json for every subdir. Sets the ``reachable`` step output."""
    failures = []
    for subdir in channel.subdirs:
        url = f"{channel.url}/{subdir}/repodata.json"
        try:
            status = f"HTTP {net.request(url).status}"
        except Error as err:
            status = str(err)
        if status != "HTTP 200":
            failures.append(f"GET {url} gave {status}")
    reachable = not failures
    set_output("reachable", "true" if reachable else "false", environ)
    if reachable:
        print(f"{channel.url} serves repodata for {', '.join(channel.subdirs)}")
    else:
        message = "Builds are dispatched once the channel serves repodata for every platform in channel.toml."
        print("Channel not reachable:", *failures, message, sep="\n")
        _append(environ, "GITHUB_STEP_SUMMARY",
                "### Channel not reachable\n\n" + "".join(f"- {line}\n" for line in failures) + f"\n{message}\n")
    return reachable


def validate_targets(channel: Channel, text: str) -> list[dict]:
    try:
        targets = json.loads(text)
    except ValueError:
        raise Error("targets must be a JSON array") from None
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise Error(f"targets must be a JSON array of 1 to {MAX_TARGETS} items")
    seen = set()
    for item in targets:
        if (not isinstance(item, dict) or set(item) != {"target", "runner"}
                or not all(isinstance(value, str) for value in item.values())):
            raise Error('each target must be {"target": "<subdir>/<Package>", "runner": "<label>"}')
        subdir, _ = parse_target(item["target"])
        if subdir not in channel.platforms:
            raise Error(f"{item['target']}: {subdir} is not configured in channel.toml")
        if item["runner"] != channel.platforms[subdir]:
            raise Error(f"{item['target']}: runner must be {channel.platforms[subdir]}")
        if item["target"] in seen:
            raise Error(f"{item['target']} is listed twice")
        seen.add(item["target"])
    return targets


def matrix(channel: Channel, text: str, label: str) -> str:
    """The validated targets as a compact JSON list for the build job matrix."""
    if not _LABEL.fullmatch(label):
        raise Error(f"invalid label {label!r}")
    return json.dumps(validate_targets(channel, text), separators=(",", ":"))


def check_builds(after_run: str = "", run=subprocess.run, environ=os.environ) -> bool:
    """Whether build runs other than ``after_run`` are active. Sets the ``active`` step output."""
    if not _RUN_ID.fullmatch(after_run):
        raise Error(f"invalid run id {after_run!r}")
    command = ["gh", "run", "list", "--workflow", "build.yml", "--json", "databaseId,status", "--limit", "200"]
    try:
        result = run(command, capture_output=True, text=True, check=True)
        runs = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError) as err:
        raise Error(f"gh run list failed: {err}") from None
    active = any(r.get("status") in ACTIVE and str(r.get("databaseId")) != after_run for r in runs)
    set_output("active", "true" if active else "false", environ)
    if active:
        message = "Builds in progress; the last one to finish triggers the next sync."
        print(message)
        _append(environ, "GITHUB_STEP_SUMMARY", f"{message}\n")
    return active


def dispatch(channel: Channel, plan: dict, dry_run: bool = False,
             run=subprocess.run, environ=os.environ) -> list[list[dict]]:
    """Start one build workflow run per shard of ready targets. Returns the shards."""
    ready = plan.get("ready", [])
    if not ready:
        print("nothing to build")
        return []

    size = channel.shard_size
    shards = [ready[i:i + size] for i in range(0, len(ready), size)]
    failed = []
    for number, shard in enumerate(shards, 1):
        label = f"{number}/{len(shards)}"
        targets = json.dumps(shard, separators=(",", ":"))
        if dry_run:
            print(f"shard {label}: {targets}")
            continue
        command = ["gh", "workflow", "run", "build.yml", "-f", f"targets={targets}", "-f", f"label={label}"]
        for delay in (0, *RETRY_DELAYS):
            sleep(delay)
            if run(command, check=False).returncode == 0:
                print(f"dispatched shard {label} with {len(shard)} targets")
                break
        else:
            failed.append(label)
        if number < len(shards):
            sleep(PAUSE)
    if failed:
        raise Error(f"could not dispatch shards {', '.join(failed)}")
    if not dry_run:
        _append(environ, "GITHUB_STEP_SUMMARY",
                f"\nDispatched {len(shards)} build runs for {len(ready)} targets.\n")
    return shards
