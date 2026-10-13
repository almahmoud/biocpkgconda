import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from biocpkgconda import __main__, metadata, plan
from biocpkgconda.config import Override

from .support import ROOT, channel

PIN = "r-base >=4.6,<4.7.0a0"

BIOC = """\
Package: Core
Version: 1.0.0
Depends: R (>= 4.1.0), methods
Imports: Tool, ggplot2
NeedsCompilation: no

Package: Native
Version: 2.0.0
Imports: Core
LinkingTo: Rcpp
NeedsCompilation: yes

Package: Top
Version: 1.0.0
Imports: Native, Core
NeedsCompilation: no

Package: Orphan
Version: 1.0.0
Imports: biclust
NeedsCompilation: no

Package: Future
Version: 1.0.0
Depends: R (>= 4.7.0)
NeedsCompilation: no

Package: Huge
Version: 1.0.0
NeedsCompilation: no

Package: UsesHuge
Version: 1.0.0
Depends: Huge
NeedsCompilation: no

Package: UsesOrphan
Version: 1.0.0
Imports: Orphan
NeedsCompilation: no
"""

CRAN = """\
Package: Tool
Version: 0.5-1
Imports: Helper
NeedsCompilation: no

Package: Helper
Version: 1.0
NeedsCompilation: yes

Package: ggplot2
Version: 4.0.3
Imports: Unused
NeedsCompilation: no

Package: Rcpp
Version: 1.1.0
NeedsCompilation: yes

Package: Unused
Version: 1.0
NeedsCompilation: no
"""

CONDA_FORGE = frozenset({"r-ggplot2", "r-rcpp", "r-unused"})
OVERRIDES = {"Huge": Override(skip="source tarball exceeds the runner disk")}


def sources():
    return metadata.Sources(metadata.parse_bioc(BIOC, "3.23", "bioc"), metadata.parse_cran(CRAN))


def built(name, version, depends=(PIN,)):
    return {"name": name, "version": version, "depends": list(depends)}


class PlannerTest(unittest.TestCase):
    def planner(self, state=None, retry_failed=False):
        state = state or {"subdirs": {}, "failures": []}
        return plan.Planner(channel(), sources(), CONDA_FORGE, OVERRIDES, state, retry_failed)

    def statuses(self, planner):
        return {f"{subdir}/{name}": result.status for (name, subdir), result in planner.plan().items()}

    def test_units(self):
        self.assertEqual(sorted(self.planner().units),
                         ["Core", "Future", "Helper", "Huge", "Native", "Orphan", "Tool", "Top", "UsesHuge",
                          "UsesOrphan"])

    def test_empty_channel(self):
        self.assertEqual(self.statuses(self.planner()), {
            "noarch/Core": "waiting",
            "noarch/Future": "unresolvable",
            "linux-64/Helper": "ready",
            "osx-arm64/Helper": "ready",
            "noarch/Huge": "skipped",
            "linux-64/Native": "waiting",
            "osx-arm64/Native": "waiting",
            "noarch/Orphan": "unresolvable",
            "noarch/Tool": "waiting",
            "noarch/Top": "waiting",
            "noarch/UsesHuge": "blocked",
            "noarch/UsesOrphan": "blocked",
        })

    def test_reasons(self):
        results = self.planner().plan()
        self.assertEqual(results[("Future", "noarch")].reason, "requires R >= 4.7.0")
        self.assertEqual(results[("Orphan", "noarch")].reason,
                         "depends on biclust, which is not in Bioconductor, CRAN or conda-forge")
        self.assertEqual(results[("UsesOrphan", "noarch")].root, "Orphan")
        self.assertEqual(results[("UsesHuge", "noarch")].reason, "source tarball exceeds the runner disk")

    def test_progress(self):
        state = {"subdirs": {"noarch": [built("r-tool", "0.5_1"), built("bioconductor-core", "1.0.0")],
                             "linux-64": [built("r-helper", "1.0")],
                             "osx-arm64": [built("r-helper", "1.0", depends=["r-base >=4.5,<4.6.0a0"])]},
                 "failures": []}
        statuses = self.statuses(self.planner(state))
        self.assertEqual(statuses["noarch/Core"], "built")
        self.assertEqual(statuses["linux-64/Helper"], "built")
        self.assertEqual(statuses["osx-arm64/Helper"], "ready")
        self.assertEqual(statuses["linux-64/Native"], "ready")
        # Core is built, but its dependency Helper has no osx-arm64 build for R 4.6 yet.
        self.assertEqual(statuses["osx-arm64/Native"], "waiting")
        self.assertEqual(statuses["noarch/Top"], "waiting")

    def test_malformed_records_are_not_built(self):
        state = {"subdirs": {"noarch": [{"name": "r-tool", "version": "0.5_1", "depends": None},
                                        {"name": "r-tool", "version": "0.5_1", "depends": [1]},
                                        {"name": ["bioconductor-core"], "version": "1.0.0", "depends": [PIN]}]},
                 "failures": []}
        statuses = self.statuses(self.planner(state))
        self.assertEqual(statuses["noarch/Tool"], "waiting")
        self.assertEqual(statuses["noarch/Core"], "waiting")

    def test_old_version_is_not_built(self):
        state = {"subdirs": {"noarch": [built("bioconductor-core", "0.9.0")]}, "failures": []}
        self.assertEqual(self.statuses(self.planner(state))["noarch/Core"], "waiting")

    def test_noarch_waits_for_compiled_dependency_on_its_build_platform(self):
        state = {"subdirs": {"noarch": [built("r-tool", "0.5_1"), built("bioconductor-core", "1.0.0")],
                             "linux-64": [built("r-helper", "1.0"), built("bioconductor-native", "2.0.0")]},
                 "failures": []}
        planner = self.planner(state)
        self.assertEqual(planner.noarch_build_subdir, "linux-64")
        statuses = self.statuses(planner)
        self.assertEqual(statuses["noarch/Top"], "ready")
        self.assertEqual(statuses["osx-arm64/Native"], "waiting")

    def test_unbuilt_noarch_is_blocked_where_a_dependency_fails(self):
        state = {"subdirs": {"noarch": [built("r-tool", "0.5_1"), built("bioconductor-core", "1.0.0")],
                             "linux-64": [built("r-helper", "1.0"), built("bioconductor-native", "2.0.0")]},
                 "failures": [{"subdir": "osx-arm64", "name": "r-helper", "version": "1.0", "attempts": 2,
                               "run_url": "x"}]}
        planner = self.planner(state)
        self.assertEqual(planner.status("Top", "osx-arm64").status, "ready")
        self.assertEqual(planner.usable("Top", "linux-64"), plan.Status("waiting"))
        self.assertEqual(planner.usable("Top", "osx-arm64"),
                         plan.Status("blocked", "build failed 2 times on osx-arm64", "Helper"))

    def test_failures(self):
        state = {"subdirs": {}, "failures": [
            {"subdir": "linux-64", "name": "r-helper", "version": "1.0", "attempts": 2, "run_url": "x"},
            {"subdir": "osx-arm64", "name": "r-helper", "version": "1.0", "attempts": 1, "run_url": "x"}]}
        planner = self.planner(state)
        results = planner.plan()
        self.assertEqual(results[("Helper", "linux-64")], plan.Status("failed", "build failed 2 times on linux-64",
                                                                       "Helper"))
        self.assertEqual(results[("Helper", "osx-arm64")].status, "ready")
        self.assertEqual(results[("Native", "linux-64")].status, "blocked")
        self.assertEqual(results[("Tool", "noarch")], plan.Status("blocked", "build failed 2 times on linux-64",
                                                                   "Helper"))
        # Core and Tool are noarch and build on linux-64, where Helper failed.
        self.assertEqual(results[("Native", "osx-arm64")].status, "blocked")
        self.assertEqual(self.statuses(self.planner(state, retry_failed=True))["linux-64/Helper"], "ready")

    def test_dependency_only_on_conda_forge(self):
        planner = plan.Planner(channel(), sources(), CONDA_FORGE | {"r-biclust"}, OVERRIDES, {})
        results = planner.plan()
        self.assertEqual(results[("Orphan", "noarch")].status, "ready")
        self.assertEqual(results[("UsesOrphan", "noarch")].status, "waiting")

    def test_dependency_cycle(self):
        bioc = "Package: A\nVersion: 1.0\nImports: B\n\nPackage: B\nVersion: 1.0\nImports: A\n"
        planner = plan.Planner(channel(), metadata.Sources(metadata.parse_bioc(bioc, "3.23", "bioc"), {}),
                               frozenset(), {}, {})
        results = planner.plan()
        self.assertEqual(results[("A", "noarch")], plan.Status("unresolvable", "dependency cycle", "A"))
        self.assertEqual(results[("B", "noarch")], plan.Status("blocked", "dependency cycle", "A"))


class OutputTest(unittest.TestCase):
    def setUp(self):
        self.channel = channel()
        self.planner = plan.Planner(self.channel, sources(), CONDA_FORGE, OVERRIDES, {"subdirs": {}, "failures": []})
        self.results = self.planner.plan()

    def test_ready(self):
        document = plan.ready(self.channel, self.results)
        self.assertEqual(document["ready"], [{"target": "linux-64/Helper", "runner": "ubuntu-24.04"},
                                             {"target": "osx-arm64/Helper", "runner": "macos-15"}])
        self.assertEqual(document["counts"]["noarch"]["waiting"], 3)
        self.assertEqual(sum(document["counts"]["linux-64"].values()), 2)

    def test_summary(self):
        text = plan.summary(self.channel, self.planner, self.results)
        self.assertIn("Bioconductor 3.23 for R 4.6: 8 Bioconductor packages and 2 CRAN packages", text)
        self.assertIn("| Status | noarch | linux-64 | osx-arm64 | Total |", text)
        self.assertIn("| ready | 0 | 1 | 1 | 2 |", text)
        self.assertIn("| Huge | source tarball exceeds the runner disk | 1 |", text)
        self.assertIn("| Future | requires R >= 4.7.0 | 0 |", text)
        self.assertLess(text.index("| Huge |"), text.index("| Future |"))

    def test_summary_is_capped(self):
        results = {(f"P{i:03}", "noarch"): plan.Status("skipped", "a | b", f"P{i:03}") for i in range(60)}
        text = plan.summary(self.channel, self.planner, results)
        self.assertIn("| P000 | a \\| b | 0 |", text)
        self.assertNotIn("| P050 |", text)
        self.assertIn("10 more not shown.", text)


class CommandTest(unittest.TestCase):
    def test_plan_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "state.json").write_text(json.dumps({"subdirs": {}, "failures": []}))
            with mock.patch.object(__main__.config, "load_channel", return_value=channel()), \
                    mock.patch.object(__main__.metadata, "load", return_value=sources()), \
                    mock.patch.object(__main__.metadata, "conda_forge_r", return_value=CONDA_FORGE), \
                    redirect_stdout(io.StringIO()) as stdout:
                code = __main__.main(["plan", "--config", str(ROOT), "--state", str(tmp / "state.json"),
                                      "--out", str(tmp / "plan.json"), "--summary", str(tmp / "summary.md")])
            self.assertEqual(code, 0)
            self.assertIn("3 targets ready", stdout.getvalue())
            self.assertEqual(len(json.loads((tmp / "plan.json").read_text())["ready"]), 3)
            self.assertTrue((tmp / "summary.md").read_text().startswith("## Channel status"))
