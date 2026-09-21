"""Render rattler-build recipes for Bioconductor and CRAN packages."""

import hashlib
import json
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import Error, dcf, licenses
from .config import Channel, Override, Rule
from .metadata import Package, Sources
from .naming import bioc_name, cran_name, exceeds_r, match_spec, r_base_pin, version_key
from .net import open_stream

LANGUAGES = {
    ".c": "c",
    ".cc": "cxx", ".cpp": "cxx", ".cxx": "cxx", ".c++": "cxx", ".C": "cxx",
    ".f": "fortran", ".f90": "fortran", ".f95": "fortran", ".for": "fortran",
    ".F": "fortran", ".F90": "fortran", ".f03": "fortran",
}

BUILD_SH = """\
#!/usr/bin/env bash
set -euo pipefail
R CMD INSTALL --build .
"""


@dataclass(frozen=True)
class SourceInfo:
    description: dict[str, str]
    has_src: bool
    languages: frozenset[str]
    files: frozenset[str]  # top-level files of the package directory


class _HashingReader:
    def __init__(self, raw):
        self.raw = raw
        self.md5 = hashlib.md5()

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.md5.update(data)
        return data


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def read_source(stream, package: str) -> tuple[SourceInfo, str]:
    """Scan a source tarball stream. Returns what the recipe needs and the md5 of the stream."""
    reader = _HashingReader(stream)
    description, has_src, languages, files = None, False, set(), set()
    try:
        with tarfile.open(fileobj=reader, mode="r|gz") as tar:
            for member in tar:
                parts = PurePosixPath(member.name.removeprefix("./")).parts
                if len(parts) < 2 or parts[0] != package:
                    continue
                if parts[1] == "src":
                    has_src = True
                    language = LANGUAGES.get(PurePosixPath(parts[-1]).suffix)
                    if member.isfile() and language:
                        languages.add(language)
                elif len(parts) == 2 and member.isfile():
                    files.add(parts[1])
                    if parts[1] == "DESCRIPTION":
                        description = tar.extractfile(member).read()
        while reader.read(1 << 20):
            pass
    except (tarfile.TarError, OSError, EOFError, zlib.error) as err:
        raise Error(f"{package}: cannot read the source tarball: {err}") from None
    if description is None:
        raise Error(f"{package}: the source tarball has no {package}/DESCRIPTION")
    try:
        fields = dcf.parse_one(_decode(description))
    except ValueError as err:
        raise Error(f"{package}: invalid DESCRIPTION: {err}") from None
    return SourceInfo(fields, has_src, frozenset(languages), frozenset(files)), reader.md5.hexdigest()


def fetch_source(package: Package) -> SourceInfo:
    """Download the source tarball and verify it against the MD5sum from PACKAGES."""
    if not package.md5:
        raise Error(f"{package.name}: PACKAGES has no MD5sum")
    with open_stream(package.url) as stream:
        info, md5 = read_source(stream, package.name)
    if md5 != package.md5:
        raise Error(f"{package.name}: md5 of {package.url} is {md5}, PACKAGES lists {package.md5}")
    return info


def dependency_name(name: str, sources: Sources) -> str:
    """Conda name of a strong dependency; one in neither Bioconductor nor CRAN comes from conda-forge."""
    return bioc_name(name) if name in sources.bioc else cran_name(name)


def merge(specs: list[str]) -> list[str]:
    """Deduplicate match specs by package name, keeping the most specific constraint."""
    constraints: dict[str, list[str]] = {}
    for spec in specs:
        name, _, constraint = (spec, "", "") if spec.startswith("${{") else spec.partition(" ")
        entry = constraints.setdefault(name, [])
        if constraint and constraint not in entry:
            entry.append(constraint)
    merged = []
    for name, entry in constraints.items():
        if not entry:
            merged.append(name)
        elif all(c.startswith(">=") and "," not in c for c in entry):
            merged.append(f"{name} {max(entry, key=lambda c: version_key(c[2:]))}")
        else:
            merged.append(f"{name} {','.join(entry)}")
    return merged


def check_buildable(package: Package, subdir: str, channel: Channel, override: Override | None) -> None:
    if override and override.skip:
        raise Error(f"{package.name} is skipped: {override.skip}")
    if package.r_version and exceeds_r(package.r_version, channel.r_version):
        raise Error(f"{package.name} requires R >= {package.r_version}; the channel builds for R {channel.r_version}")
    if subdir not in channel.platforms:
        raise Error(f"{subdir} is not configured in channel.toml [platforms]")
    if subdir == "noarch" and package.needs_compilation:
        raise Error(f"{package.name} needs compilation; build it for a platform subdir, not noarch")
    if subdir != "noarch" and not package.needs_compilation:
        raise Error(f"{package.name} does not need compilation; build it for noarch")


def render(package: Package, info: SourceInfo, subdir: str, channel: Channel, sources: Sources,
           rules: tuple[Rule, ...], override: Override | None) -> dict:
    compiled = package.needs_compilation or info.has_src
    if compiled and subdir == "noarch":
        raise Error(f"{package.name} contains src/; build it for a platform subdir, not noarch")

    depends = [match_spec(dependency_name(d.name, sources), d) for d in package.depends]
    linking = [match_spec(dependency_name(d.name, sources), d) for d in package.linking_to]
    build: list[str] = []
    if compiled:
        build += ["${{ compiler('c') }}", "${{ stdlib('c') }}"]
        build += [f"${{{{ compiler('{language}') }}}}" for language in ("cxx", "fortran") if language in info.languages]
        build += ["make", "pkg-config"]
    host = ["r-base", *depends, *linking]
    run = [r_base_pin(channel.r_version), *depends]

    system_requirements = " ".join(info.description.get("SystemRequirements", "").split())
    for rule in rules:
        if rule.applies(system_requirements, subdir):
            build += rule.build
            host += rule.host
            run += rule.run
    if override:
        build += override.build
        host += override.host
        run += override.run
    if compiled:
        host.append("zlib")

    license = licenses.convert(info.description.get("License") or package.license)
    license_files = [f"${{{{ PREFIX }}}}/lib/R/share/licenses/{name}" for name in license.r_files]
    license_files += [name for name in license.source_files if name in info.files]

    build_section: dict = {"number": 0}
    if not compiled:
        build_section["noarch"] = "generic"
    build_section["script"] = "build.sh"
    if compiled:
        build_section["dynamic_linking"] = {"rpaths": ["lib/R/lib/", "lib/"]}

    requirements = {"build": merge(build)} if build else {}
    requirements |= {"host": merge(host), "run": merge(run)}

    about = {"homepage": package.homepage, "license": license.spdx}
    if license_files:
        about["license_file"] = license_files
    # rattler-build evaluates ${{ }} in every string of the recipe.
    about["summary"] = " ".join(info.description.get("Title", "").split()).replace("${{", "$ {{") or package.name

    return {
        "package": {"name": package.conda_name, "version": package.conda_version},
        "source": {"url": package.url, "md5": package.md5},
        "build": build_section,
        "requirements": requirements,
        "tests": [{"r": {"libraries": [package.name]}}],
        "about": about,
    }


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # A JSON string is a valid double-quoted YAML scalar.
    return json.dumps(value)


def dump_yaml(data: dict, indent: int = 0) -> str:
    """Emit YAML for nested dicts, lists and scalars."""
    pad = "  " * indent
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(dump_yaml(value, indent + 1))
        elif isinstance(value, list):
            if not value:
                lines.append(f"{pad}{key}: []")
                continue
            lines.append(f"{pad}{key}:")
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{pad}  - " + dump_yaml(item, indent + 2).lstrip())
                else:
                    lines.append(f"{pad}  - {_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_scalar(value)}")
    return "\n".join(lines)


def write(recipe: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "recipe.yaml").write_text(dump_yaml(recipe) + "\n")
    script = out / "build.sh"
    script.write_text(BUILD_SH)
    script.chmod(0o755)


def generate(target: tuple[str, str], channel: Channel, sources: Sources, rules: tuple[Rule, ...],
             overrides: dict[str, Override], out: Path) -> dict:
    """Write the recipe for a target."""
    subdir, name = target
    package = sources.get(name)
    if package is None:
        raise Error(f"{name} is not in Bioconductor {channel.bioc_version} or CRAN")
    override = overrides.get(name)
    check_buildable(package, subdir, channel, override)
    recipe = render(package, fetch_source(package), subdir, channel, sources, rules, override)
    write(recipe, out)
    return recipe
