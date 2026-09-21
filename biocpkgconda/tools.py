"""Install and run rattler-build."""

import hashlib
import platform
import re
import subprocess
from pathlib import Path

from . import Error
from .config import Channel
from .net import fetch

RATTLER_BUILD = "https://github.com/prefix-dev/rattler-build/releases/download/v{version}/rattler-build-{target}"
PINNING = ("https://raw.githubusercontent.com/conda-forge/conda-forge-pinning-feedstock/{commit}"
           "/recipe/conda_build_config.yaml")

# rattler-build refuses -c channels when the variant config sets channel_sources.
_CHANNEL_SOURCES = re.compile(rb"^channel_sources:.*\n(?:[ \t]+.*\n|[ \t]*\n)*", re.MULTILINE)

TARGETS = {
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("Linux", "aarch64"): "aarch64-unknown-linux-musl",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
}


def rattler_build_target(system: str | None = None, machine: str | None = None) -> str:
    key = (system or platform.system(), machine or platform.machine())
    if key not in TARGETS:
        raise Error(f"no rattler-build release for {key[0]} {key[1]}")
    return TARGETS[key]


def install_rattler_build(channel: Channel, dest: Path, target: str | None = None) -> Path:
    """Download the pinned rattler-build binary and verify its sha256 from channel.toml."""
    target = target or rattler_build_target()
    expected = channel.rattler_build_sha256.get(target)
    if not expected:
        raise Error(f"channel.toml has no rattler-build sha256 for {target}")
    data = fetch(RATTLER_BUILD.format(version=channel.rattler_build_version, target=target))
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise Error(f"rattler-build for {target} has sha256 {actual}, channel.toml expects {expected}")
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "rattler-build"
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def fetch_pinning(channel: Channel, dest: Path) -> Path:
    """Write conda-forge's conda_build_config.yaml at the pinned commit, without channel_sources."""
    data = _CHANNEL_SOURCES.sub(b"", fetch(PINNING.format(commit=channel.pinning)))
    if re.search(rb"^channel_sources\b", data, re.MULTILINE):
        raise Error("cannot remove channel_sources from conda_build_config.yaml")
    dest.mkdir(parents=True, exist_ok=True)
    # rattler-build reads conda-build syntax only from a file with exactly this name.
    path = dest / "conda_build_config.yaml"
    path.write_bytes(data)
    return path


def build_command(channel: Channel, recipe: Path, output_dir: Path, variant_config: Path,
                  markdown_summary: Path | None = None) -> list[str]:
    command = ["rattler-build", "build", "--recipe", str(recipe), "--output-dir", str(output_dir),
               "-m", str(variant_config), "--variant", f"r_base={channel.r_version}", "-c", channel.url]
    for upstream in channel.upstream:
        command += ["-c", upstream]
    command += ["--channel-priority", "strict", "--package-format", "conda"]
    if markdown_summary:
        command += ["--markdown-summary", str(markdown_summary)]
    return command


def build(channel: Channel, recipe: Path, output_dir: Path, variant_config: Path,
          markdown_summary: Path | None = None, run=subprocess.run) -> int:
    command = build_command(channel, recipe, output_dir, variant_config, markdown_summary)
    print("+ " + " ".join(command), flush=True)
    try:
        return run(command, check=False).returncode
    except FileNotFoundError:
        raise Error("rattler-build is not installed or not on PATH") from None
