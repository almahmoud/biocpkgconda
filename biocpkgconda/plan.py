"""Decide which packages are ready to build, and summarize the channel."""

from collections import defaultdict
from dataclasses import dataclass

from .config import Channel, Override
from .metadata import Package, Sources
from .naming import SUBDIRS, cran_name, exceeds_r

STATUSES = ("built", "skipped", "unresolvable", "failed", "blocked", "waiting", "ready")
_STOPPED = {"skipped", "unresolvable", "failed", "blocked"}
SUMMARY_ROWS = 50


def _is_build(record: dict, pin: str) -> bool:
    """Whether a channel state record is a well-formed build that depends on ``pin``."""
    depends = record.get("depends")
    return (isinstance(record.get("name"), str) and isinstance(record.get("version"), str)
            and isinstance(depends, list) and any(isinstance(d, str) and d.startswith(pin) for d in depends))


@dataclass(frozen=True)
class Status:
    status: str
    reason: str = ""
    root: str | None = None  # the skipped, unresolvable or failed package behind this status


class Planner:
    def __init__(self, channel: Channel, sources: Sources, conda_forge: frozenset[str],
                 overrides: dict[str, Override], state: dict, retry_failed: bool = False):
        self.channel = channel
        self.sources = sources
        self.conda_forge = conda_forge
        self.overrides = overrides
        self.retry_failed = retry_failed
        pin = f"r-base >={channel.r_version},"
        self.built = {
            subdir: {(r["name"], r["version"]) for r in records if _is_build(r, pin)}
            for subdir, records in state.get("subdirs", {}).items()
        }
        self.failures = {(f["subdir"], f["name"], f["version"]): f["attempts"] for f in state.get("failures", [])}
        self.units = self._units()
        self._memo: dict[tuple[str, str], Status | None] = {}
        self._usable: dict[tuple[str, str], Status | None] = {}

    def _units(self) -> dict[str, Package]:
        """Bioconductor packages plus the CRAN packages they need that conda-forge lacks."""
        units = dict(self.sources.bioc)
        pending = [name for package in units.values() for name in package.strong_dependencies]
        while pending:
            name = pending.pop()
            package = self.sources.cran.get(name)
            if name in units or package is None or package.conda_name in self.conda_forge:
                continue
            units[name] = package
            pending.extend(package.strong_dependencies)
        return units

    def subdirs(self, package: Package) -> tuple[str, ...]:
        return self.channel.build_subdirs if package.needs_compilation else ("noarch",)

    @property
    def noarch_build_subdir(self) -> str:
        """The platform whose compiled packages noarch builds install: the one sharing the noarch runner."""
        runner = self.channel.platforms["noarch"]
        return next(s for s in self.channel.build_subdirs if self.channel.platforms[s] == runner)

    def _is_built(self, package: Package, subdir: str) -> bool:
        key = (package.conda_name, package.conda_version)
        return key in self.built.get(subdir, ()) or key in self.built.get("noarch", ())

    def _build_platform(self, package: Package, platform: str) -> str:
        return platform if package.needs_compilation else self.noarch_build_subdir

    def _memoized(self, memo: dict, key: tuple, evaluate) -> Status:
        if key in memo:
            return memo[key] or Status("unresolvable", "dependency cycle", key[0])
        memo[key] = None
        memo[key] = result = evaluate()
        return result

    def status(self, name: str, platform: str) -> Status:
        """Build status of a package; noarch packages are built on ``noarch_build_subdir``."""
        package = self.units[name]
        key = (name, self._build_platform(package, platform))
        return self._memoized(self._memo, key, lambda: self._evaluate(package, key[1]))

    def usable(self, name: str, platform: str) -> Status:
        """``built`` once the package and all its dependencies in this channel install on ``platform``.

        Blocked as soon as one of those dependencies cannot install there, even before the package is built.
        """
        return self._memoized(self._usable, (name, platform), lambda: self._check_usable(self.units[name], platform))

    def _check_usable(self, package: Package, platform: str) -> Status:
        own = self.status(package.name, platform)
        if own.status in _STOPPED:
            return own
        return self._dependencies(package, platform) or Status("built" if own.status == "built" else "waiting")

    def _dependencies(self, package: Package, platform: str) -> Status | None:
        """Blocked or waiting when a dependency in this channel is not usable on ``platform``."""
        waiting = False
        for name in package.strong_dependencies:
            if name not in self.units:
                continue
            result = self.usable(name, platform)
            if result.status in _STOPPED:
                if result.root == package.name:
                    return Status("unresolvable", "dependency cycle", package.name)
                return Status("blocked", result.reason, result.root)
            waiting = waiting or result.status != "built"
        return Status("waiting") if waiting else None

    def _evaluate(self, package: Package, platform: str) -> Status:
        subdir = platform if package.needs_compilation else "noarch"
        if self._is_built(package, subdir):
            return Status("built")
        override = self.overrides.get(package.name)
        if override and override.skip:
            return Status("skipped", override.skip, package.name)
        if package.r_version and exceeds_r(package.r_version, self.channel.r_version):
            return Status("unresolvable", f"requires R >= {package.r_version}", package.name)
        for name in package.strong_dependencies:
            if name not in self.units and name not in self.sources.cran and cran_name(name) not in self.conda_forge:
                return Status("unresolvable", f"depends on {name}, which is not in Bioconductor, CRAN or conda-forge",
                              package.name)
        count = self.failures.get((subdir, package.conda_name, package.conda_version), 0)
        if count >= self.channel.max_attempts and not self.retry_failed:
            return Status("failed", f"build failed {count} times on {subdir}", package.name)
        return self._dependencies(package, platform) or Status("ready")

    def plan(self) -> dict[tuple[str, str], Status]:
        """Status of every (package, subdir) the channel builds."""
        return {(name, subdir): self.status(name, subdir)
                for name in sorted(self.units, key=str.lower)
                for subdir in self.subdirs(self.units[name])}


def ready(channel: Channel, results: dict[tuple[str, str], Status]) -> dict:
    """The plan.json document: ready targets with their runners, and counts per subdir and status."""
    targets = sorted((key for key, result in results.items() if result.status == "ready"),
                     key=lambda key: (SUBDIRS.index(key[1]), key[0].lower()))
    counts: dict[str, dict[str, int]] = {s: dict.fromkeys(STATUSES, 0) for s in channel.subdirs}
    for (_, subdir), result in results.items():
        counts[subdir][result.status] += 1
    return {"ready": [{"target": f"{subdir}/{name}", "runner": channel.platforms[subdir]} for name, subdir in targets],
            "counts": counts}


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def summary(channel: Channel, planner: Planner, results: dict[tuple[str, str], Status]) -> str:
    counts = ready(channel, results)["counts"]
    cran = sum(1 for package in planner.units.values() if not package.is_bioc)
    lines = [
        "## Channel status",
        "",
        f"Bioconductor {channel.bioc_version} for R {channel.r_version}: "
        f"{len(planner.units) - cran} Bioconductor packages and {cran} CRAN packages missing from conda-forge.",
        "",
        "| Status | " + " | ".join(channel.subdirs) + " | Total |",
        "|---|" + "--:|" * (len(channel.subdirs) + 1),
    ]
    for status in STATUSES:
        row = [counts[subdir][status] for subdir in channel.subdirs]
        lines.append(f"| {status} | " + " | ".join(map(str, row)) + f" | {sum(row)} |")

    reasons: dict[str, dict[str, None]] = defaultdict(dict)
    dependents: dict[str, set[str]] = defaultdict(set)
    for (name, _), result in results.items():
        if result.status in ("skipped", "unresolvable", "failed"):
            reasons[name][result.reason] = None
        elif result.status == "blocked":
            dependents[result.root].add(name)
    if reasons:
        roots = sorted(reasons, key=lambda name: (-len(dependents[name]), name.lower()))
        lines += ["", "### Packages that are not built", "",
                  "| Package | Reason | Blocked dependents |", "|---|---|--:|"]
        for name in roots[:SUMMARY_ROWS]:
            lines.append(f"| {name} | {_cell('; '.join(reasons[name]))} | {len(dependents[name])} |")
        if len(roots) > SUMMARY_ROWS:
            lines += ["", f"{len(roots) - SUMMARY_ROWS} more not shown."]
    return "\n".join(lines) + "\n"
