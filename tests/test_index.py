import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from biocpkgconda import index
from biocpkgconda.config import REGISTRY_DESCRIPTION
from biocpkgconda.naming import failure_tag, package_tag
from biocpkgconda.registry import FAILURE, INDEX, PACKAGE_V2, REPODATA, REPODATA_ZST

from .support import FakeRegistry, requires_zstandard

SOURCE = "https://github.com/example/channel"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def record(name, version, subdir, build="r46_0"):
    return {"name": name, "version": version, "build": build, "build_number": 0, "subdir": subdir,
            "depends": ["r-base >=4.6,<4.7.0a0"]}


class Channel:
    """Builds manifests and blobs the way the publish and fail commands push them."""

    def __init__(self):
        self.registry = FakeRegistry()

    def blob(self, data: bytes) -> str:
        self.registry.blobs[digest(data)] = data
        return digest(data)

    def add_package(self, subdir, fields, title=None):
        filename = f"{fields['name']}-{fields['version']}-{fields['build']}.conda"
        package = f"bytes of {filename}".encode()
        info = json.dumps(fields).encode()
        self.registry.manifests[package_tag(subdir, filename)] = {"layers": [
            {"mediaType": PACKAGE_V2, "digest": digest(package), "size": len(package),
             "annotations": {"org.opencontainers.image.title": title or filename,
                             "org.conda.md5": hashlib.md5(package).hexdigest()}},
            {"mediaType": INDEX, "digest": self.blob(info), "size": len(info)},
        ]}
        return filename, package

    def add_repodata(self, subdir, repodata):
        data = index.serialize(repodata)
        self.registry.manifests[f"{subdir}_repodata"] = {"layers": [
            {"mediaType": REPODATA, "digest": self.blob(data), "size": len(data)},
            {"mediaType": REPODATA_ZST, "digest": "sha256:" + "0" * 64, "size": 1},
        ]}

    def add_failure(self, subdir, name, version, attempts):
        self.registry.manifests[failure_tag(subdir, name, version)] = {
            "layers": [{"mediaType": FAILURE, "digest": "sha256:" + "1" * 64, "size": 1}],
            "annotations": {"org.conda.subdir": subdir, "org.conda.name": name, "org.conda.version": version,
                            "org.biocpkgconda.attempts": str(attempts), "org.biocpkgconda.run-url": "https://x/1"}}


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.channel = Channel()
        self.stderr = io.StringIO()
        self.enterContext(redirect_stderr(self.stderr))

    def reconcile(self, subdir):
        return index.reconcile(self.channel.registry, subdir, self.channel.registry.list_tags())

    def test_new_packages_are_added(self):
        filename, package = self.channel.add_package("linux-64", record("bioconductor-deseq2", "1.52.0", "linux-64"))
        self.channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        repodata, data, changed, added = self.reconcile("linux-64")
        self.assertTrue(changed)
        self.assertEqual(added, 1)
        self.assertEqual(list(repodata["packages.conda"]), [filename])
        entry = repodata["packages.conda"][filename]
        self.assertEqual(entry["md5"], hashlib.md5(package).hexdigest())
        self.assertEqual(entry["sha256"], hashlib.sha256(package).hexdigest())
        self.assertEqual(entry["size"], len(package))
        self.assertEqual(entry["depends"], ["r-base >=4.6,<4.7.0a0"])
        self.assertEqual(repodata["info"], {"subdir": "linux-64"})
        self.assertEqual(json.loads(data), repodata)
        self.assertNotIn(b"\": ", data)

    def test_unchanged_repodata_is_not_pushed_again(self):
        self.channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        repodata, *_ = self.reconcile("noarch")
        self.channel.add_repodata("noarch", repodata)
        requested = len(self.channel.registry.requested)
        _, _, changed, added = self.reconcile("noarch")
        self.assertFalse(changed)
        self.assertEqual(added, 0)
        self.assertEqual(self.channel.registry.requested[requested:], ["noarch_repodata"])

    def test_only_new_records_are_counted(self):
        self.channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        repodata, *_ = self.reconcile("noarch")
        self.channel.add_repodata("noarch", repodata)
        self.channel.add_package("noarch", record("r-foo", "1.1", "noarch"))
        self.channel.add_package("noarch", record("r-bar", "1.0", "noarch"))
        repodata, _, changed, added = self.reconcile("noarch")
        self.assertTrue(changed)
        self.assertEqual(added, 2)
        self.assertEqual(len(repodata["packages.conda"]), 3)

    def test_deleted_tags_are_removed(self):
        filename, _ = self.channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        repodata, *_ = self.reconcile("noarch")
        self.channel.add_repodata("noarch", repodata)
        del self.channel.registry.manifests[package_tag("noarch", filename)]
        repodata, _, changed, added = self.reconcile("noarch")
        self.assertTrue(changed)
        self.assertEqual(added, 0)
        self.assertEqual(repodata["packages.conda"], {})

    def test_missing_repodata_is_created(self):
        repodata, _, changed, _ = self.reconcile("osx-arm64")
        self.assertTrue(changed)
        self.assertEqual(repodata, index.empty_repodata("osx-arm64"))

    def test_mismatched_manifests_are_skipped(self):
        self.channel.add_package("linux-64", record("r-foo", "1.0", "noarch"))
        self.channel.add_package("linux-64", record("r-bar", "1.0", "linux-64"), title="r-other-1.0-r46_0.conda")
        repodata, *_ = self.reconcile("linux-64")
        self.assertEqual(repodata["packages.conda"], {})
        self.assertIn("r-foo-1.0-r46_0.conda is built for 'noarch', not 'linux-64'", self.stderr.getvalue())
        self.assertIn("does not belong to this tag", self.stderr.getvalue())

    def test_incomplete_package_layer_is_skipped(self):
        filename, _ = self.channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        del self.channel.registry.manifests[package_tag("noarch", filename)]["layers"][0]["size"]
        repodata, *_ = self.reconcile("noarch")
        self.assertEqual(repodata["packages.conda"], {})
        self.assertIn("lacks an md5 annotation, sha256 digest or size", self.stderr.getvalue())

    def test_index_json_fields(self):
        filename, _ = self.channel.add_package("noarch", dict(record("r-foo", "1.0", "noarch"), junk="x",
                                                              track_features=7))
        self.channel.add_package("noarch", dict(record("r-bar", "1.0", "noarch"), depends=None))
        self.channel.add_package("noarch", dict(record("r-big", "1.0", "noarch"), junk="x" * 70000))
        repodata, *_ = self.reconcile("noarch")
        self.assertEqual(list(repodata["packages.conda"]), [filename])
        self.assertNotIn("junk", repodata["packages.conda"][filename])
        self.assertNotIn("track_features", repodata["packages.conda"][filename])
        self.assertIn("r-bar-1.0-r46_0.conda: info/index.json has an invalid depends", self.stderr.getvalue())
        self.assertIn("index.json layer has no size or is larger than 65536 bytes", self.stderr.getvalue())

    def test_invalid_json_is_skipped(self):
        self.channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        tag = package_tag("noarch", "r-foo-1.0-r46_0.conda")
        info = self.channel.registry.manifests[tag]["layers"][1]
        for data in (b"{", b"[" * 60000):
            self.channel.registry.blobs[info["digest"]] = data
            repodata, *_ = self.reconcile("noarch")
            self.assertEqual(repodata["packages.conda"], {})
        self.assertIn("info/index.json is not a JSON object", self.stderr.getvalue())

    def test_stored_records_are_checked_again(self):
        good, _ = self.channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        bad, _ = self.channel.add_package("noarch", dict(record("r-bar", "1.0", "noarch"), depends=None))
        repodata, *_ = self.reconcile("noarch")
        repodata["packages.conda"][good]["junk"] = "x"
        repodata["packages.conda"][bad] = dict(record("r-bar", "1.0", "noarch"), depends=None, md5="0" * 32,
                                               sha256="0" * 64, size=1)
        self.channel.add_repodata("noarch", repodata)
        repodata, _, changed, added = self.reconcile("noarch")
        self.assertTrue(changed)
        self.assertEqual(added, 0)
        self.assertEqual(list(repodata["packages.conda"]), [good])
        self.assertNotIn("junk", repodata["packages.conda"][good])

    def test_prefixes_do_not_overlap(self):
        tags = ["osx-64_a-1-0.conda", "osx-arm64_b-1-0.conda", "osx-64_repodata", "failed_osx-64_a-1"]
        self.assertEqual(index.package_tags(tags, "osx-64"), {"osx-64_a-1-0.conda"})


class FailuresTest(unittest.TestCase):
    def test_markers(self):
        channel = Channel()
        channel.add_failure("linux-64", "bioconductor-deseq2", "1.52.0", 2)
        channel.registry.manifests["failed_linux-64_bogus-1"] = {"annotations": {}}
        with redirect_stderr(io.StringIO()):
            markers = index.failures(channel.registry, channel.registry.list_tags())
        self.assertEqual(markers, [{"subdir": "linux-64", "name": "bioconductor-deseq2", "version": "1.52.0",
                                    "attempts": 2, "run_url": "https://x/1"}])


class IndexTest(unittest.TestCase):
    def test_state_and_pushes(self):
        channel = Channel()
        channel.add_package("noarch", record("r-foo", "1.0", "noarch"))
        channel.add_failure("linux-64", "bioconductor-deseq2", "1.52.0", 1)
        pushed = []
        with mock.patch.object(index, "push_repodata", lambda registry, subdir, data, source, run: pushed.append(
                (subdir, json.loads(data)))), redirect_stdout(io.StringIO()):
            state, added = index.index(channel.registry, ("noarch", "linux-64"), SOURCE)
        self.assertEqual(added, 1)
        self.assertEqual([subdir for subdir, _ in pushed], ["noarch", "linux-64"])
        self.assertEqual(state["subdirs"], {
            "noarch": [{"name": "r-foo", "version": "1.0", "depends": ["r-base >=4.6,<4.7.0a0"]}],
            "linux-64": [],
        })
        self.assertEqual(state["failures"][0]["attempts"], 1)


@requires_zstandard
class PushTest(unittest.TestCase):
    def test_push_repodata(self):
        import zstandard

        calls = []

        def oras(command, cwd, check):
            calls.append((command, {p.name: p.read_bytes() for p in Path(cwd).iterdir()}))

        index.push_repodata(FakeRegistry(), "noarch", b'{"a":1}', SOURCE, run=oras)
        [(command, files)] = calls
        self.assertEqual(command[4:8], ["ghcr.io/example/channel:noarch_repodata",
                                        "repodata.json:application/vnd.conda.repodata.v1+json",
                                        "repodata.json.zst:application/vnd.conda.repodata.v1+json+zst",
                                        "--annotation-file"])
        self.assertEqual(files["repodata.json"], b'{"a":1}')
        self.assertEqual(zstandard.ZstdDecompressor().decompress(files["repodata.json.zst"]), b'{"a":1}')
        self.assertEqual(json.loads(files["annotations.json"]),
                         {"$manifest": {"org.opencontainers.image.source": SOURCE,
                                        "org.opencontainers.image.description": REGISTRY_DESCRIPTION}})
