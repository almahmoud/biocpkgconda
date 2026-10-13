import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from biocpkgconda import __main__, index, metadata, plan, status
from biocpkgconda.config import Override

from .support import ROOT, channel

PIN = "r-base >=4.6,<4.7.0a0"
RUN = "https://github.com/example/channel/actions/runs/"
NOW = datetime(2026, 10, 13, 8, 30, 5, tzinfo=timezone.utc)

BIOC = """\
Package: Core
Version: 1.0.0
Depends: R (>= 4.1.0), methods
Imports: Helper, ggplot2
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

Package: UsesHuge
Version: 1.0.0
Depends: Huge
NeedsCompilation: no

Package: Broken
Version: 1.0.0
NeedsCompilation: yes
"""

EXPERIMENT = """\
Package: Huge
Version: 1.0.0
NeedsCompilation: no
"""

CRAN = """\
Package: Helper
Version: 1.0-2
NeedsCompilation: yes

Package: ggplot2
Version: 4.0.3
NeedsCompilation: no

Package: Rcpp
Version: 1.1.0
NeedsCompilation: yes
"""

CONDA_FORGE = frozenset({"r-ggplot2", "r-rcpp"})
SKIP = "source tarball exceeds the runner disk"
OVERRIDES = {"Huge": Override(skip=SKIP)}
PLATFORMS = ("linux-64", "osx-arm64")


def sources():
    return metadata.Sources(metadata.parse_bioc(BIOC, "3.23", "bioc")
                            | metadata.parse_bioc(EXPERIMENT, "3.23", "data/experiment"),
                            metadata.parse_cran(CRAN))


def built(name, version):
    return {"name": name, "version": version, "depends": [PIN]}


def failure(subdir, name, version, attempts, run, stage=None):
    marker = {"subdir": subdir, "name": name, "version": version, "attempts": attempts, "run_url": f"{RUN}{run}"}
    return marker if stage is None else dict(marker, stage=stage)


STATE = {
    "subdirs": {"noarch": [built("bioconductor-core", "1.0.0")],
                "linux-64": [built("r-helper", "1.0_2"), built("bioconductor-native", "2.0.0")],
                "osx-arm64": []},
    "failures": [
        failure("osx-arm64", "r-helper", "1.0_2", 2, 1, "check"),
        failure("osx-arm64", "bioconductor-broken", "1.0.0", 1, 2, "recipe"),
        failure("linux-64", "bioconductor-broken", "1.0.0", 2, 3, "build"),
        # A marker without a stage.
        failure("noarch", "bioconductor-top", "1.0.0", 1, 4),
        failure("linux-64", "bioconductor-broken", "0.9.0", 2, 5, "build"),
        failure("win-64", "bioconductor-broken", "1.0.0", 2, 6, "build"),
        failure("linux-64", "bioconductor-gone", "1.0", 2, 7, "build"),
    ],
}


def planner(state=STATE):
    return plan.Planner(channel(), sources(), CONDA_FORGE, OVERRIDES, state)


def cell(install, build=None, reason="", root=None):
    return {"install": install, "build": build or install, "reason": reason, "root": root}


HELPER_FAILED = "build failed 2 times on osx-arm64"

EXPECTED = {
    "Broken": {"linux-64": cell("failed", reason="build failed 2 times on linux-64", root="Broken"),
               "osx-arm64": cell("waiting", "ready")},
    "Core": {"linux-64": cell("built"), "osx-arm64": cell("blocked", "built", HELPER_FAILED, "Helper")},
    "Helper": {"linux-64": cell("built"), "osx-arm64": cell("failed", reason=HELPER_FAILED, root="Helper")},
    "Huge": {platform: cell("skipped", reason=SKIP, root="Huge") for platform in PLATFORMS},
    "Native": {"linux-64": cell("built"), "osx-arm64": cell("blocked", reason=HELPER_FAILED, root="Helper")},
    # Top builds once on linux-64, but Native, which it imports, cannot install on osx-arm64.
    "Top": {"linux-64": cell("waiting", "ready"), "osx-arm64": cell("blocked", "ready", HELPER_FAILED, "Helper")},
    "UsesHuge": {platform: cell("blocked", reason=SKIP, root="Huge") for platform in PLATFORMS},
}


class DocumentTest(unittest.TestCase):
    def setUp(self):
        self.document = status.document(planner(), STATE["failures"], NOW)

    def test_shape(self):
        document = self.document
        self.assertEqual(set(document), {"schema", "generated", "channel", "bioconductor", "r", "platforms",
                                         "packages", "failures"})
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["generated"], "2026-10-13T08:30:05Z")
        self.assertEqual(document["channel"], "https://conda.biocpkgcontainers.org")
        self.assertEqual((document["bioconductor"], document["r"]), ("3.23", "4.6"))
        self.assertEqual(document["platforms"], ["linux-64", "osx-arm64"])
        for name, package in document["packages"].items():
            with self.subTest(name):
                self.assertEqual(set(package), {"version", "repo", "noarch", "depends", "platforms"})
                self.assertIn(package["repo"], ("bioc", "data/annotation", "data/experiment", "workflows", "cran"))
                self.assertIsInstance(package["noarch"], bool)
                self.assertEqual(list(package["platforms"]), document["platforms"])
                for entry in package["platforms"].values():
                    self.assertEqual(set(entry), {"install", "build", "reason", "root"})
                    self.assertIn(entry["install"], plan.STATUSES)
                    self.assertIn(entry["build"], plan.STATUSES)
                    self.assertIsInstance(entry["reason"], str)
                    self.assertTrue(entry["root"] is None or entry["root"] in document["packages"])
        for name, markers in document["failures"].items():
            self.assertIn(name, document["packages"])
            for marker in markers:
                self.assertEqual(set(marker), {"subdir", "version", "attempts", "run_url", "stage"})

    def test_every_unit_is_listed(self):
        self.assertEqual(list(self.document["packages"]), sorted(planner().units))
        self.assertEqual(set(self.document["packages"]), set(EXPECTED))
        self.assertNotIn("ggplot2", self.document["packages"])

    def test_packages(self):
        packages = self.document["packages"]
        self.assertEqual(packages["Helper"] | {"platforms": None},
                         {"version": "1.0-2", "repo": "cran", "noarch": False, "depends": [], "platforms": None})
        self.assertEqual(packages["Huge"]["repo"], "data/experiment")
        self.assertTrue(packages["Huge"]["noarch"])
        self.assertEqual(packages["Core"]["repo"], "bioc")
        self.assertTrue(packages["Core"]["noarch"])
        self.assertFalse(packages["Native"]["noarch"])
        self.assertEqual(packages["Core"]["depends"], ["Helper"])
        self.assertEqual(packages["Native"]["depends"], ["Core"])
        self.assertEqual(packages["Top"]["depends"], ["Native", "Core"])
        self.assertEqual(packages["UsesHuge"]["depends"], ["Huge"])

    def test_platforms(self):
        for name, platforms in EXPECTED.items():
            with self.subTest(name):
                self.assertEqual(self.document["packages"][name]["platforms"], platforms)

    def test_failures_of_current_versions(self):
        self.assertEqual(self.document["failures"], {
            "Broken": [{"subdir": "linux-64", "version": "1.0.0", "attempts": 2, "run_url": f"{RUN}3",
                        "stage": "build"},
                       {"subdir": "osx-arm64", "version": "1.0.0", "attempts": 1, "run_url": f"{RUN}2",
                        "stage": "recipe"}],
            "Helper": [{"subdir": "osx-arm64", "version": "1.0_2", "attempts": 2, "run_url": f"{RUN}1",
                        "stage": "check"}],
            "Top": [{"subdir": "noarch", "version": "1.0.0", "attempts": 1, "run_url": f"{RUN}4", "stage": ""}],
        })

    def test_generated_now(self):
        document = status.document(planner(), [])
        self.assertRegex(document["generated"], r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        self.assertEqual(document["failures"], {})

    def test_serialize(self):
        text = status.serialize(self.document)
        self.assertEqual(json.loads(text), self.document)
        self.assertIsNone(re.search(r'[:,] |\n', text))
        self.assertEqual(list(json.loads(text)), sorted(self.document))
        shuffled = dict(STATE, failures=STATE["failures"][::-1])
        self.assertEqual(status.serialize(status.document(planner(shuffled), shuffled["failures"], NOW)), text)


class CommandTest(unittest.TestCase):
    def run_status(self, *argv, environ=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "state.json").write_text(json.dumps(STATE))
            argv = [arg.replace("{tmp}", str(tmp)) for arg in argv]
            with mock.patch.dict(os.environ, environ or {}), \
                    mock.patch.object(__main__.config, "load_channel", return_value=channel()), \
                    mock.patch.object(__main__.config, "load_overrides", return_value=OVERRIDES), \
                    mock.patch.object(__main__.metadata, "load", return_value=sources()), \
                    mock.patch.object(__main__.metadata, "conda_forge_r", return_value=CONDA_FORGE), \
                    redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                code = __main__.main(["status", "--config", str(ROOT), "--out", str(tmp / "status.json"), *argv])
            output = tmp / "status.json"
            document = json.loads(output.read_text()) if output.exists() else None
        return code, document, stdout.getvalue(), stderr.getvalue()

    def test_status_from_state_file(self):
        with mock.patch.object(index, "channel_state", side_effect=AssertionError("registry read")):
            code, document, stdout, _ = self.run_status("--state", "{tmp}/state.json")
        self.assertEqual(code, 0)
        self.assertIn("with 7 packages", stdout)
        self.assertEqual(document["packages"]["Core"]["platforms"], EXPECTED["Core"])
        self.assertEqual(sorted(document["failures"]), ["Broken", "Helper", "Top"])

    def test_status_from_registry(self):
        environ = {"GITHUB_REPOSITORY": "example/channel", "GITHUB_ACTOR": "someone", "REGISTRY_TOKEN": "token"}
        with mock.patch.object(__main__, "Registry") as registry, \
                mock.patch.object(index, "channel_state", return_value=STATE) as channel_state:
            code, document, _, _ = self.run_status(environ=environ)
        self.assertEqual(code, 0)
        registry.assert_called_once_with("ghcr.io", "example/channel")
        registry.from_environment.assert_not_called()
        channel_state.assert_called_once_with(registry.return_value, ("noarch", "linux-64", "osx-arm64"))
        self.assertEqual(document["packages"]["Native"]["platforms"], EXPECTED["Native"])

    def test_unreadable_state(self):
        code, document, _, stderr = self.run_status("--state", "{tmp}/missing.json")
        self.assertEqual(code, 1)
        self.assertIsNone(document)
        self.assertIn("cannot read", stderr)
