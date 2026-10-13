import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from biocpkgconda import Error, __main__, config, record
from biocpkgconda.config import REGISTRY_DESCRIPTION

from .support import ROOT, FakeRegistry, Symlink, conda_package, paths_json, requires_zstandard, sources, zst_tar

INDEX = {"name": "bioconductor-deseq2", "version": "1.52.0", "build": "r46h1234567_0", "build_number": 0,
         "subdir": "linux-64", "depends": ["r-base >=4.6,<4.7.0a0"]}
FILENAME = "bioconductor-deseq2-1.52.0-r46h1234567_0.conda"


class Oras:
    """Records oras invocations with the working directory contents at call time."""

    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, command, cwd, check):
        files = {p.name: p.read_bytes() for p in Path(cwd).iterdir()}
        self.calls.append((command, files))
        if self.returncode:
            raise subprocess.CalledProcessError(self.returncode, command)


class IndexRecordTest(unittest.TestCase):
    def test_keeps_repodata_fields(self):
        index = dict(INDEX, license="LGPL-3.0-or-later", timestamp=1767225600000, constrains=["r-foo <2"],
                     junk="x" * 1000, track_features="a b")
        self.assertEqual(record.index_record(index, FILENAME, "linux-64"),
                         dict(INDEX, license="LGPL-3.0-or-later", timestamp=1767225600000, constrains=["r-foo <2"]))

    def test_mismatches(self):
        with self.assertRaisesRegex(Error, "built for 'linux-64', not 'osx-arm64'"):
            record.index_record(INDEX, FILENAME, "osx-arm64")
        with self.assertRaisesRegex(Error, "does not match the name, version and build"):
            record.index_record(dict(INDEX, build="r46h7654321_0"), FILENAME, "linux-64")
        with self.assertRaisesRegex(Error, "not a JSON object"):
            record.index_record(None, FILENAME, "linux-64")
        with self.assertRaisesRegex(Error, "built for a value that is not a string, not 'linux-64'"):
            record.index_record(dict(INDEX, subdir=json.loads("[" * 900 + "]" * 900)), FILENAME, "linux-64")

    def test_invalid_fields(self):
        for field, value in [("depends", None), ("depends", [1]), ("depends", "r-base"), ("constrains", {}),
                             ("build_number", "0"), ("build_number", True), ("timestamp", -1), ("license", 1),
                             ("noarch", "python")]:
            with self.subTest(field=field, value=value), self.assertRaisesRegex(Error, f"invalid {field}"):
                record.index_record(dict(INDEX, **{field: value}), FILENAME, "linux-64")
        with self.assertRaisesRegex(Error, "has no depends"):
            record.index_record({k: v for k, v in INDEX.items() if k != "depends"}, FILENAME, "linux-64")


class PackagePathsTest(unittest.TestCase):
    def paths(self, *entries):
        return record.package_paths(json.dumps({"paths": list(entries)}).encode(), FILENAME, "DESeq2")

    def test_paths(self):
        self.assertEqual(self.paths({"_path": "lib/R/library/DESeq2/R/DESeq2", "path_type": "hardlink"},
                                    {"_path": "lib/R/library/DESeq2/doc", "path_type": "directory"}),
                         {"lib/R/library/DESeq2/R/DESeq2": "hardlink", "lib/R/library/DESeq2/doc": "directory"})

    def test_paths_outside_the_package_directory(self):
        for path in ["lib/R/library/Biobase/R/Biobase.rdb", "bin/.bioconductor-deseq2-post-link.sh",
                     "etc/conda/activate.d/deseq2.sh", "lib/R/library/DESeq2Extra/x",
                     "lib/R/library/DESeq2/../Biobase/R/Biobase.rdb", "/lib/R/library/DESeq2/x",
                     "lib/R/library/DESeq2//x", "lib/R/library/DESeq2/x\\..\\..\\Biobase", "lib/R/library/DESeq2/\n",
                     None]:
            with self.subTest(path=path), self.assertRaisesRegex(Error, "outside lib/R/library/DESeq2/"):
                self.paths({"_path": path, "path_type": "hardlink"})

    def test_rejections(self):
        with self.assertRaisesRegex(Error, "path type 'unix_python_entry_point'"):
            self.paths({"_path": "lib/R/library/DESeq2/x", "path_type": "unix_python_entry_point"})
        with self.assertRaisesRegex(Error, "listed twice"):
            self.paths(*[{"_path": "lib/R/library/DESeq2/x", "path_type": "hardlink"}] * 2)
        with self.assertRaisesRegex(Error, "info/paths.json not found"):
            record.package_paths(None, FILENAME, "DESeq2")
        with self.assertRaisesRegex(Error, "no list of paths"):
            record.package_paths(b"[]", FILENAME, "DESeq2")


@requires_zstandard
class CheckTest(unittest.TestCase):
    def check(self, index=INDEX, **package):
        with tempfile.TemporaryDirectory() as tmp:
            return record.check([conda_package(Path(tmp), index, **package)], ("linux-64", "DESeq2"), sources())

    def test_valid_package(self):
        payload = {"lib/R/library/DESeq2": None, "lib/R/library/DESeq2/R": None,
                   "lib/R/library/DESeq2/R/DESeq2.rdb": b"data", "lib/R/library/DESeq2/libs/DESeq2.so": b"elf",
                   "lib/R/library/DESeq2/libs/current.so": Symlink("DESeq2.so")}
        [(path, fields)] = self.check(dict(INDEX, junk="x"), payload=payload)
        self.assertEqual(path.name, FILENAME)
        self.assertEqual(fields, INDEX)

    def test_read_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = record.read_info(conda_package(Path(tmp), INDEX))
        self.assertEqual(json.loads(info["info/index.json"]), INDEX)
        self.assertEqual(json.loads(info["info/paths.json"])["paths"],
                         [{"_path": "lib/R/library/DESeq2/DESCRIPTION", "path_type": "hardlink"}])

    def test_not_a_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / FILENAME
            path.write_bytes(b"not a zip")
            with self.assertRaisesRegex(Error, "cannot read"):
                record.read_info(path)

    def test_file_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bioconductor-deseq2-1.52.0-r46h1234567_0.tar.bz2"
            path.write_bytes(b"")
            with self.assertRaisesRegex(Error, "not a valid"):
                record.check([path], ("linux-64", "DESeq2"), sources())
        with self.assertRaisesRegex(Error, "does not exist"):
            record.check([Path("work/out/linux-64/*.conda")], ("linux-64", "DESeq2"), sources())

    def test_other_package(self):
        with self.assertRaisesRegex(Error, "not a build of bioconductor-deseq2 1.52.0"):
            self.check(dict(INDEX, name="bioconductor-limma"))
        with self.assertRaisesRegex(Error, "not a build of bioconductor-deseq2 1.52.0"):
            self.check(dict(INDEX, version="1.50.1"))
        with self.assertRaisesRegex(Error, "invalid depends"):
            self.check(dict(INDEX, depends=None))

    def test_check_package_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = conda_package(Path(tmp), INDEX)
            bad = conda_package(Path(tmp), dict(INDEX, version="1.50.1"))
            with mock.patch.object(__main__.metadata, "load", return_value=sources()), \
                    redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(__main__.main(["check-package", "--config", str(ROOT), "--target", "linux-64/DESeq2",
                                                str(path)]), 0)
                self.assertEqual(__main__.main(["check-package", "--config", str(ROOT), "--target", "linux-64/DESeq2",
                                                str(bad)]), 1)
        self.assertIn(f"{FILENAME} is a valid build of linux-64/DESeq2", stdout.getvalue())
        self.assertIn("not a build of bioconductor-deseq2 1.52.0", stderr.getvalue())

    def test_extra_archive_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = conda_package(Path(tmp), INDEX)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("extra.tar.zst", zst_tar({"lib/R/library/Biobase/R/Biobase.rdb": b"x"}))
            with self.assertRaisesRegex(Error, "must hold exactly"):
                record.check([path], ("linux-64", "DESeq2"), sources())

    def test_info_rejections(self):
        cases = [
            ({"info/index.json": json.dumps(dict(INDEX, junk="x" * record.MAX_INDEX_SIZE)).encode()},
             "info/index.json is larger than 65536 bytes"),
            ({"info/index.json": None}, "info/index.json not found"),
            ({"info/paths.json": None}, "info/paths.json not found"),
            ({"info/link.json": b"{}"}, "has info/link.json; conda creates entry points"),
            ({"info/Paths.json": b"{}"}, "'info/Paths.json' in the info archive clashes with info/paths.json"),
            ({"./info/paths.json": b"{}"}, "unexpected entry './info/paths.json'"),
            ({"lib/R/library/Biobase/x": b""}, "unexpected entry 'lib/R/library/Biobase/x'"),
        ]
        for info, message in cases:
            with self.subTest(info=list(info)), self.assertRaisesRegex(Error, message):
                self.check(info=info)

    def test_payload_rejections(self):
        listed = {"lib/R/library/DESeq2/DESCRIPTION": b"Package: DESeq2\n"}
        cases = [
            (listed | {"lib/R/library/DESeq2/up": Symlink("../Biobase")}, None,
             "links to '../Biobase', outside its directory"),
            (listed | {"lib/R/library/DESeq2/root": Symlink("/etc")}, None, "links to '/etc', outside its directory"),
            (listed | {"lib/R/library/Biobase": None}, None, "'lib/R/library/Biobase' in the package does not match"),
            (listed | {"lib/R/library/Biobase/R/Biobase.rdb": b"x"}, listed,
             "'lib/R/library/Biobase/R/Biobase.rdb' in the package does not match"),
            (listed | {"lib/R/library/DESeq2/x": b"x"}, listed | {"lib/R/library/DESeq2/x": Symlink("y")},
             "'lib/R/library/DESeq2/x' in the package does not match"),
            ({}, listed, "DESCRIPTION is in info/paths.json but not in the package"),
        ]
        for payload, paths, message in cases:
            info = {"info/paths.json": paths_json(paths)} if paths is not None else None
            with self.subTest(message=message), self.assertRaisesRegex(Error, message):
                self.check(payload=payload, info=info)


@requires_zstandard
class PublishTest(unittest.TestCase):
    def publish(self, registry=None, index=INDEX):
        registry = registry or FakeRegistry()
        oras = Oras()
        with tempfile.TemporaryDirectory() as tmp:
            path = conda_package(Path(tmp), index)
            with mock.patch("sys.stdout"):
                record.publish([path], ("linux-64", "DESeq2"), registry, sources(),
                               "https://github.com/example/channel", run=oras)
            return oras.calls, path.read_bytes()

    def test_push(self):
        [(command, files)], package = self.publish(index=dict(INDEX, junk="x" * 1000))
        tag = "linux-64_" + FILENAME
        self.assertEqual(command, ["oras", "push", "--image-spec", "v1.0", f"ghcr.io/example/channel:{tag}",
                                   f"{FILENAME}:application/vnd.conda.package.v2",
                                   "index.json:application/vnd.conda.info.index.v1+json",
                                   "--annotation-file", "annotations.json"])
        self.assertEqual(files[FILENAME], package)
        self.assertEqual(json.loads(files["index.json"]), INDEX)
        self.assertEqual(json.loads(files["annotations.json"]), {
            FILENAME: {"org.conda.md5": hashlib.md5(package).hexdigest()},
            "$manifest": {"org.opencontainers.image.source": "https://github.com/example/channel",
                          "org.opencontainers.image.description": REGISTRY_DESCRIPTION,
                          "org.conda.subdir": "linux-64", "org.conda.filename": FILENAME},
        })

    def test_already_published(self):
        existing = FakeRegistry({"linux-64_" + FILENAME: {"layers": []}})
        with mock.patch("builtins.print") as output:
            self.assertEqual(self.publish(registry=existing)[0], [])
        self.assertIn("::notice::linux-64/" + FILENAME + " is already published", output.call_args.args[0])


class OrasTest(unittest.TestCase):
    def test_oras_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(Error, "exit code 1"):
                record.oras_push("ghcr.io/a/b:t", [], {}, Path(tmp), run=Oras(returncode=1))
            with self.assertRaisesRegex(Error, "not on PATH"):
                record.oras_push("ghcr.io/a/b:t", [], {}, Path(tmp), run=mock.Mock(side_effect=FileNotFoundError))


class FailTest(unittest.TestCase):
    def fail(self, registry, stage="build"):
        oras = Oras()
        with mock.patch("sys.stdout"):
            count = record.fail(("linux-64", "DESeq2"), "https://github.com/example/channel/actions/runs/1", stage,
                                registry, sources(), "https://github.com/example/channel", run=oras)
        return count, oras.calls

    def test_first_failure(self):
        count, [(command, files)] = self.fail(FakeRegistry())
        self.assertEqual(count, 1)
        self.assertEqual(command[4], "ghcr.io/example/channel:failed_linux-64_bioconductor-deseq2-1.52.0")
        self.assertEqual(command[5], "failure.json:application/vnd.biocpkgconda.failure.v1+json")
        self.assertEqual(json.loads(files["failure.json"]), {
            "name": "bioconductor-deseq2", "version": "1.52.0", "subdir": "linux-64", "attempts": 1,
            "run_url": "https://github.com/example/channel/actions/runs/1", "stage": "build"})
        annotations = json.loads(files["annotations.json"])["$manifest"]
        self.assertEqual(annotations["org.biocpkgconda.attempts"], "1")
        self.assertEqual(annotations["org.conda.name"], "bioconductor-deseq2")
        self.assertEqual(annotations["org.conda.version"], "1.52.0")
        self.assertEqual(annotations["org.conda.subdir"], "linux-64")
        self.assertEqual(annotations["org.biocpkgconda.run-url"], "https://github.com/example/channel/actions/runs/1")
        self.assertEqual(annotations["org.biocpkgconda.stage"], "build")
        self.assertEqual(annotations["org.opencontainers.image.description"], REGISTRY_DESCRIPTION)

    def test_increments_attempts(self):
        tag = "failed_linux-64_bioconductor-deseq2-1.52.0"
        registry = FakeRegistry({tag: {"annotations": {"org.biocpkgconda.attempts": "1"}}})
        self.assertEqual(self.fail(registry)[0], 2)

    def test_stages(self):
        for stage in record.STAGES:
            with self.subTest(stage):
                [(_, files)] = self.fail(FakeRegistry(), stage)[1]
                self.assertEqual(json.loads(files["failure.json"])["stage"], stage)
                self.assertEqual(json.loads(files["annotations.json"])["$manifest"]["org.biocpkgconda.stage"], stage)

    def test_invalid_stage(self):
        for stage in ("", "publish", "Build", "build\n", "recipe build"):
            oras = Oras()
            with self.subTest(stage), self.assertRaisesRegex(Error, "invalid stage"):
                record.fail(("linux-64", "DESeq2"), "https://github.com/example/channel/actions/runs/1", stage,
                            FakeRegistry(), sources(), "x", run=oras)
            self.assertEqual(oras.calls, [])

    def test_attempts(self):
        self.assertEqual(record.attempts(None), 0)
        self.assertEqual(record.attempts({"annotations": {"org.biocpkgconda.attempts": "x"}}), 0)
        self.assertEqual(record.attempts({"annotations": {"org.biocpkgconda.attempts": "3"}}), 3)

    def test_invalid_run_url(self):
        with self.assertRaisesRegex(Error, "invalid run URL"):
            record.fail(("linux-64", "DESeq2"), "javascript:alert(1)", "build", FakeRegistry(), sources(), "x",
                        run=Oras())

    def test_fail_command(self):
        environ = {"GITHUB_REPOSITORY": "example/channel"}
        argv = ["fail", "--config", str(ROOT), "--target", "osx-arm64/DESeq2", "--run-url", "https://x/1"]
        with mock.patch.dict("os.environ", environ), \
                mock.patch.object(__main__.metadata, "load", return_value=sources()), \
                mock.patch.object(__main__.record, "fail") as fail:
            self.assertEqual(__main__.main([*argv, "--stage", "check"]), 0)
            with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
                __main__.main([*argv, "--stage", "publish"])
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                __main__.main(argv)
        fail.assert_called_once()
        self.assertEqual(fail.call_args.args[:3], (("osx-arm64", "DESeq2"), "https://x/1", "check"))
        self.assertIn("invalid choice: 'publish'", stderr.getvalue())


class SourceUrlTest(unittest.TestCase):
    def test_source_url(self):
        self.assertEqual(config.source_url("almahmoud/biocpkgconda", {}), "https://github.com/almahmoud/biocpkgconda")
        self.assertEqual(config.source_url("x/y", {"GITHUB_SERVER_URL": "https://ghe.example/",
                                                   "GITHUB_REPOSITORY": "Org/Repo"}), "https://ghe.example/Org/Repo")
