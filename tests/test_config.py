import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from biocpkgconda import Error, __main__, config

ROOT = Path(__file__).resolve().parent.parent
CHANNEL = (ROOT / "channel.toml").read_text()
IMAGE = '# image = "ghcr.io/almahmoud/biocpkgconda-builder:3.23"'


def load(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "channel.toml"
        path.write_text(text)
        return config.load_channel(path)


class ChannelTest(unittest.TestCase):
    def test_repository_defaults(self):
        channel = config.load_channel(ROOT / "channel.toml")
        self.assertEqual(channel.url, "https://conda.biocpkgcontainers.org")
        self.assertEqual(channel.upstream, ("conda-forge",))
        self.assertIsNone(channel.registry)
        self.assertEqual((channel.bioc_version, channel.r_version), ("3.23", "4.6"))
        self.assertEqual(channel.repositories, ("bioc", "data/annotation", "data/experiment", "workflows"))
        self.assertEqual(channel.platforms, {"noarch": "ubuntu-24.04", "linux-64": "ubuntu-24.04",
                                             "osx-arm64": "macos-15"})
        self.assertEqual(channel.subdirs, ("noarch", "linux-64", "osx-arm64"))
        self.assertEqual(channel.build_subdirs, ("linux-64", "osx-arm64"))
        self.assertEqual((channel.shard_size, channel.max_attempts), (100, 2))
        self.assertIsNone(channel.image)
        self.assertEqual(channel.rattler_build_version, "0.76.1")
        self.assertEqual(set(channel.rattler_build_sha256),
                         {"x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl", "aarch64-apple-darwin"})

    def test_invalid_values(self):
        cases = {
            'osx-arm64 = "macos-15"': 'osx-arm65 = "macos-15"',
            'noarch = "ubuntu-24.04"\n': "",
            'noarch = "ubuntu-24.04"': 'noarch = "ubuntu-24.04-arm"',
            'linux-64 = "ubuntu-24.04"\n': "",
            'shard_size = 100': "shard_size = 121",
            'max_attempts = 2': "max_attempts = 0",
            'version = "3.23"': 'version = 3.23',
            '"workflows"]': '"books"]',
            'url = "https://conda.biocpkgcontainers.org"': 'url = "http://example.org"',
            'url = "https://conda.biocpkgcontainers.org"\n': 'url = "https://conda.example.org/bioc"\n',
            '# registry = "ghcr.io/almahmoud/biocpkgconda"': 'registry = "ghcr.io/Almahmoud/biocpkgconda"',
            '# registry = "ghcr.io/almahmoud/biocpkgconda"\n': 'registry = "ghcr.io:5000/almahmoud/biocpkgconda"\n',
            'pinning = "b7dd': 'pinning = "zzzz',
        }
        for old, new in cases.items():
            with self.subTest(new=new):
                self.assertIn(old, CHANNEL)
                with self.assertRaises(Error):
                    load(CHANNEL.replace(old, new, 1))

    def test_rejects_anaconda_repository(self):
        for name in ("defaults", "anaconda", "main", "r", "pkgs/main", "https://repo.anaconda.com/pkgs/main",
                     "https://conda.anaconda.org/anaconda"):
            with self.subTest(name=name), self.assertRaisesRegex(Error, "paid license"):
                load(CHANNEL.replace('upstream = ["conda-forge"]', f'upstream = ["conda-forge", "{name}"]', 1))
        for name in ("conda-forge", "bioconda", "https://conda.anaconda.org/conda-forge", "https://prefix.dev/conda-forge"):
            with self.subTest(name=name):
                self.assertIn(name, load(CHANNEL.replace('upstream = ["conda-forge"]', f'upstream = ["{name}"]', 1)).upstream)

    def test_invalid_image(self):
        for image in ('"ghcr.io/almahmoud/biocpkgconda-builder"', '"ghcr.io/Almahmoud/biocpkgconda-builder:3.23"',
                      '"ghcr.io:5000/almahmoud/biocpkgconda-builder:3.23"', '"biocpkgconda-builder:3.23"',
                      '"ghcr.io/almahmoud/biocpkgconda-builder@sha256:abc"', "3.23"):
            with self.subTest(image=image), self.assertRaises(Error):
                load(CHANNEL.replace(IMAGE, f"image = {image}"))

    def test_largest_shard(self):
        self.assertEqual(load(CHANNEL.replace("shard_size = 100", "shard_size = 120", 1)).shard_size, 120)

    def test_only_noarch(self):
        text = CHANNEL.replace('linux-64 = "ubuntu-24.04"\n', "").replace('osx-arm64 = "macos-15"\n', "")
        with self.assertRaisesRegex(Error, "besides noarch"):
            load(text)

    def test_unreadable(self):
        with self.assertRaises(Error):
            load("[channel\n")


class RegistryReferenceTest(unittest.TestCase):
    def test_from_environment(self):
        channel = config.load_channel(ROOT / "channel.toml")
        self.assertEqual(config.registry_reference(channel, {"GITHUB_REPOSITORY": "AlMahmoud/BiocPkgConda"}),
                         ("ghcr.io", "almahmoud/biocpkgconda"))
        with self.assertRaises(Error):
            config.registry_reference(channel, {})

    def test_configured(self):
        channel = load(CHANNEL.replace('# registry = "ghcr.io/almahmoud/biocpkgconda"',
                                       'registry = "ghcr.io/example/conda-channel"'))
        self.assertEqual(config.registry_reference(channel, {"GITHUB_REPOSITORY": "other/repo"}),
                         ("ghcr.io", "example/conda-channel"))


class BuilderImageTest(unittest.TestCase):
    def test_from_environment(self):
        channel = config.load_channel(ROOT / "channel.toml")
        self.assertEqual(config.builder_image(channel, {"GITHUB_REPOSITORY": "AlMahmoud/BiocPkgConda"}),
                         "ghcr.io/almahmoud/biocpkgconda-builder:3.23")
        with self.assertRaises(Error):
            config.builder_image(channel, {})

    def test_configured(self):
        digest = "sha256:" + "0" * 64
        for image in ("ghcr.io/example/builder:3.23-R-4.6.1", f"ghcr.io/example/builder@{digest}",
                      f"quay.io/example/conda/builder:v1@{digest}"):
            with self.subTest(image=image):
                channel = load(CHANNEL.replace(IMAGE, f'image = "{image}"'))
                self.assertEqual(config.builder_image(channel, {"GITHUB_REPOSITORY": "other/repo"}), image)

    def test_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            environ = {"GITHUB_REPOSITORY": "almahmoud/biocpkgconda", "GITHUB_OUTPUT": f"{tmp}/output"}
            with mock.patch.dict(os.environ, environ):
                self.assertEqual(__main__.main(["builder-image", "--config", str(ROOT)]), 0)
            self.assertEqual(Path(environ["GITHUB_OUTPUT"]).read_text(),
                             "image=ghcr.io/almahmoud/biocpkgconda-builder:3.23\n")
