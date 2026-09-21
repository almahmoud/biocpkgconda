import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from biocpkgconda import Error, tools

from .support import channel


class RattlerBuildTest(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(tools.rattler_build_target("Linux", "x86_64"), "x86_64-unknown-linux-musl")
        self.assertEqual(tools.rattler_build_target("Linux", "aarch64"), "aarch64-unknown-linux-musl")
        self.assertEqual(tools.rattler_build_target("Darwin", "arm64"), "aarch64-apple-darwin")
        with self.assertRaises(Error):
            tools.rattler_build_target("Windows", "AMD64")

    def install(self, data, target="x86_64-unknown-linux-musl"):
        settings = channel()
        settings.rattler_build_sha256["x86_64-unknown-linux-musl"] = hashlib.sha256(b"binary").hexdigest()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tools, "fetch", return_value=data) as fetch:
            path = tools.install_rattler_build(settings, Path(tmp) / "bin", target)
            return fetch.call_args.args[0], path.read_bytes(), path.stat().st_mode

    def test_install_verifies_checksum(self):
        url, data, mode = self.install(b"binary")
        self.assertEqual(url, "https://github.com/prefix-dev/rattler-build/releases/download/v0.76.1/"
                              "rattler-build-x86_64-unknown-linux-musl")
        self.assertEqual(data, b"binary")
        self.assertTrue(mode & 0o111)
        with self.assertRaisesRegex(Error, "sha256"):
            self.install(b"tampered")
        with self.assertRaisesRegex(Error, "no rattler-build sha256"):
            self.install(b"binary", target="x86_64-apple-darwin")

    def test_fetch_pinning(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tools, "fetch", return_value=b"c: 1\n") as fetch:
            path = tools.fetch_pinning(channel(), Path(tmp))
            self.assertEqual(path.name, "conda_build_config.yaml")
            self.assertEqual(path.read_bytes(), b"c: 1\n")
        self.assertEqual(fetch.call_args.args[0],
                         "https://raw.githubusercontent.com/conda-forge/conda-forge-pinning-feedstock/"
                         "b7dd4c111e066f157c7fd6f98ec5e1760534db94/recipe/conda_build_config.yaml")

    def test_fetch_pinning_drops_channel_sources(self):
        upstream = (b"cdt_name:  # [linux]\n  - conda  # [linux]\n\nchannel_sources:\n  - conda-forge\n\n"
                    b"channel_targets:\n  - conda-forge main\n\nr_base:\n  - 4.4\n")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tools, "fetch", return_value=upstream):
            data = tools.fetch_pinning(channel(), Path(tmp)).read_bytes()
        self.assertEqual(data, b"cdt_name:  # [linux]\n  - conda  # [linux]\n\n"
                               b"channel_targets:\n  - conda-forge main\n\nr_base:\n  - 4.4\n")


class BuildTest(unittest.TestCase):
    def test_command(self):
        command = tools.build_command(channel(), Path("work/recipe"), Path("work/out"), Path("/tmp/cbc/c.yaml"),
                                      Path("/tmp/summary.md"))
        self.assertEqual(command, [
            "rattler-build", "build", "--recipe", "work/recipe", "--output-dir", "work/out",
            "-m", "/tmp/cbc/c.yaml", "--variant", "r_base=4.6",
            "-c", "https://conda.biocpkgcontainers.org", "-c", "conda-forge",
            "--channel-priority", "strict", "--package-format", "conda",
            "--markdown-summary", "/tmp/summary.md"])

    def test_returns_exit_code(self):
        run = mock.Mock(return_value=mock.Mock(returncode=3))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tools.build(channel(), Path("r"), Path("o"), Path("c"), run=run), 3)
        self.assertEqual(run.call_args.args[0][:2], ["rattler-build", "build"])
