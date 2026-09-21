import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from biocpkgconda import Error, actions, config, net

from .support import channel


def targets(*items):
    return json.dumps([{"target": t, "runner": r} for t, r in items])


class MatrixTest(unittest.TestCase):
    def test_matrix(self):
        text = targets(("linux-64/DESeq2", "ubuntu-24.04"), ("noarch/BSgenome.Hsapiens.UCSC.hg38", "ubuntu-24.04"))
        self.assertEqual(actions.matrix(channel(), text, "1/3"),
                         '[{"target":"linux-64/DESeq2","runner":"ubuntu-24.04"},'
                         '{"target":"noarch/BSgenome.Hsapiens.UCSC.hg38","runner":"ubuntu-24.04"}]')

    def test_rejections(self):
        cases = {
            "not json": "[",
            "empty": "[]",
            "object": "{}",
            "too many": targets(*[(f"noarch/P{i}", "ubuntu-24.04") for i in range(actions.MAX_TARGETS + 1)]),
            "extra key": json.dumps([{"target": "noarch/A", "runner": "ubuntu-24.04", "x": 1}]),
            "bad target": targets(("noarch/$(id)", "ubuntu-24.04")),
            "unconfigured subdir": targets(("win-64/A", "windows-2025")),
            "wrong runner": targets(("osx-arm64/A", "ubuntu-24.04")),
            "duplicate": targets(("noarch/A", "ubuntu-24.04"), ("noarch/A", "ubuntu-24.04")),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(Error):
                actions.matrix(channel(), text, "manual")
        with self.assertRaisesRegex(Error, "invalid label"):
            actions.matrix(channel(), targets(("noarch/A", "ubuntu-24.04")), "$(id)")


    def test_shard_size_limit(self):
        self.assertEqual(actions.MAX_TARGETS, config.MAX_SHARD_SIZE)
        text = targets(*[(f"noarch/P{i}", "ubuntu-24.04") for i in range(actions.MAX_TARGETS)])
        self.assertEqual(len(json.loads(actions.matrix(channel(), text, "manual"))), actions.MAX_TARGETS)


class OutputTest(unittest.TestCase):
    def test_step_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            environ = {"GITHUB_OUTPUT": f"{tmp}/output", "GITHUB_STEP_SUMMARY": f"{tmp}/summary"}
            with mock.patch.object(net, "request", return_value=net.Response(404, {}, b"")), \
                    redirect_stdout(io.StringIO()):
                self.assertFalse(actions.check_channel(channel(), environ))
            self.assertEqual(Path(environ["GITHUB_OUTPUT"]).read_text(), "reachable=false\n")
            self.assertIn("HTTP 404", Path(environ["GITHUB_STEP_SUMMARY"]).read_text())
            with mock.patch.object(net, "request", return_value=net.Response(200, {}, b"{}")), \
                    redirect_stdout(io.StringIO()):
                self.assertTrue(actions.check_channel(channel(), environ))
            self.assertEqual(Path(environ["GITHUB_OUTPUT"]).read_text(), "reachable=false\nreachable=true\n")

    def test_every_subdir_is_checked(self):
        def request(url):
            return net.Response(404 if "/osx-arm64/" in url else 200, {}, b"{}")

        with tempfile.TemporaryDirectory() as tmp:
            environ = {"GITHUB_STEP_SUMMARY": f"{tmp}/summary"}
            with mock.patch.object(net, "request", side_effect=request) as fake, redirect_stdout(io.StringIO()):
                self.assertFalse(actions.check_channel(channel(), environ))
            summary = Path(environ["GITHUB_STEP_SUMMARY"]).read_text()
        self.assertEqual([c.args[0] for c in fake.call_args_list],
                         [f"https://conda.biocpkgcontainers.org/{s}/repodata.json"
                          for s in ("noarch", "linux-64", "osx-arm64")])
        self.assertIn("- GET https://conda.biocpkgcontainers.org/osx-arm64/repodata.json gave HTTP 404\n", summary)
        self.assertNotIn("linux-64", summary)

    def test_unreachable_host(self):
        with mock.patch.object(net, "request", side_effect=Error("GET x failed after 4 attempts: timed out")), \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertFalse(actions.check_channel(channel(), {}))
        self.assertIn("reachable=false", stdout.getvalue())


class Gh:
    """Fake gh CLI: canned `run list` output and dispatch results."""

    def __init__(self, runs=(), dispatch_results=()):
        self.runs = list(runs)
        self.results = list(dispatch_results)
        self.dispatched = []

    def __call__(self, command, **kwargs):
        if command[:3] == ["gh", "run", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.runs), "")
        self.dispatched.append(command)
        return subprocess.CompletedProcess(command, self.results.pop(0) if self.results else 0)


def ready(count):
    return {"ready": [{"target": f"noarch/P{i}", "runner": "ubuntu-24.04"} for i in range(count)]}


class CheckBuildsTest(unittest.TestCase):
    def check(self, gh, after_run=""):
        with tempfile.TemporaryDirectory() as tmp:
            environ = {"GITHUB_OUTPUT": f"{tmp}/output", "GITHUB_STEP_SUMMARY": f"{tmp}/summary"}
            with redirect_stdout(io.StringIO()):
                active = actions.check_builds(after_run, run=gh, environ=environ)
            output = Path(environ["GITHUB_OUTPUT"]).read_text()
            summary = Path(environ["GITHUB_STEP_SUMMARY"]).read_text() if active else ""
        return active, output, summary

    def test_active(self):
        for status in sorted(actions.ACTIVE):
            with self.subTest(status):
                active, output, summary = self.check(Gh(runs=[{"databaseId": 9, "status": status}]), after_run="8")
                self.assertTrue(active)
                self.assertEqual(output, "active=true\n")
                self.assertIn("Builds in progress", summary)

    def test_idle(self):
        gh = Gh(runs=[{"databaseId": 7, "status": "completed"}, {"databaseId": 8, "status": "in_progress"}])
        self.assertEqual(self.check(gh, after_run="8"), (False, "active=false\n", ""))
        self.assertEqual(gh.dispatched, [])

    def test_invalid_run_id(self):
        with self.assertRaisesRegex(Error, "invalid run id"):
            actions.check_builds("1; rm -rf /", run=Gh(), environ={})


@mock.patch.object(actions, "sleep", lambda seconds: None)
class DispatchTest(unittest.TestCase):
    def dispatch(self, gh, count=250, **kwargs):
        with redirect_stdout(io.StringIO()) as stdout:
            shards = actions.dispatch(channel(), ready(count), run=gh, environ={}, **kwargs)
        return shards, stdout.getvalue()

    def test_shards(self):
        gh = Gh()
        shards, _ = self.dispatch(gh)
        self.assertEqual([len(s) for s in shards], [100, 100, 50])
        self.assertEqual(len(gh.dispatched), 3)
        command = gh.dispatched[2]
        self.assertEqual(command[:4], ["gh", "workflow", "run", "build.yml"])
        self.assertEqual(command[-1], "label=3/3")
        self.assertEqual(json.loads(command[5].removeprefix("targets=")), shards[2])

    def test_retries(self):
        gh = Gh(dispatch_results=[1, 1, 0])
        shards, _ = self.dispatch(gh, count=10)
        self.assertEqual(len(shards), 1)
        self.assertEqual(len(gh.dispatched), 3)

    def test_gives_up(self):
        gh = Gh(dispatch_results=[1] * 5 + [0])
        with self.assertRaisesRegex(Error, "could not dispatch shards 1/2"):
            self.dispatch(gh, count=150)
        self.assertEqual(len(gh.dispatched), 6)

    def test_dry_run(self):
        gh = Gh()
        shards, output = self.dispatch(gh, dry_run=True)
        self.assertEqual(len(shards), 3)
        self.assertEqual(gh.dispatched, [])
        self.assertIn("shard 1/3: [", output)

    def test_nothing_ready(self):
        gh = Gh()
        self.assertEqual(self.dispatch(gh, count=0)[0], [])
