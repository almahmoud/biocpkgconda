import contextlib
import hashlib
import io
import unittest
import urllib.error
import urllib.request

from biocpkgconda import Error, mirror
from biocpkgconda.naming import package_tag, repodata_tag
from biocpkgconda.registry import PACKAGE_V2, REPODATA, REPODATA_ZST

from .support import FakeRegistry

FILENAME = "bioconductor-deseq2-1.50.0-r46h2b5f3a1_0.conda"
PACKAGE = b"conda package" * 1000
REPODATA_JSON = b'{"packages.conda": {}}'


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def registry(title=FILENAME):
    manifests = {
        repodata_tag("linux-64"): {"layers": [
            {"mediaType": REPODATA, "digest": digest(REPODATA_JSON)},
            {"mediaType": REPODATA_ZST, "digest": digest(b"zst")},
        ]},
        package_tag("linux-64", FILENAME): {"layers": [
            {"mediaType": PACKAGE_V2, "digest": digest(PACKAGE), "size": len(PACKAGE),
             "annotations": {"org.opencontainers.image.title": title}},
        ]},
    }
    blobs = {digest(REPODATA_JSON): REPODATA_JSON, digest(b"zst"): b"zst", digest(PACKAGE): PACKAGE}
    return FakeRegistry(manifests, blobs)


def get(url: str, method: str = "GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, dict(err.headers), err.read()


class MirrorTest(unittest.TestCase):
    def serve(self, fake):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        return stack.enter_context(mirror.serve(fake, ("noarch", "linux-64")))

    def test_repodata_is_read_once(self):
        fake = registry()
        url = self.serve(fake)
        for _ in range(2):
            status, headers, body = get(url + "linux-64/repodata.json")
            self.assertEqual((status, body), (200, REPODATA_JSON))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(fake.requested, [repodata_tag("linux-64")])
        self.assertEqual(get(url + "linux-64/repodata.json.zst")[2], b"zst")

    def test_package(self):
        url = self.serve(registry())
        status, headers, body = get(url + "linux-64/" + FILENAME)
        self.assertEqual((status, body), (200, PACKAGE))
        self.assertEqual(headers["Content-Length"], str(len(PACKAGE)))
        status, headers, body = get(url + "linux-64/" + FILENAME, method="HEAD")
        self.assertEqual((status, headers["Content-Length"], body), (200, str(len(PACKAGE)), b""))

    def test_missing(self):
        url = self.serve(registry())
        for path in ("noarch/repodata.json", "osx-arm64/repodata.json", "linux-64/" + FILENAME[:-6] + ".tar.bz2",
                     "linux-64/x.conda", "linux-64/repodata.jlap", "linux-64", "a/b/c", "../linux-64/x"):
            with self.subTest(path=path):
                self.assertEqual(get(url + path)[0], 404)

    def test_package_layer_must_name_the_file(self):
        url = self.serve(registry(title="other-1.0-0.conda"))
        self.assertEqual(get(url + "linux-64/" + FILENAME)[0], 404)

    def test_registry_errors_are_bad_gateway(self):
        fake = registry()
        fake.manifest = lambda tag: (_ for _ in ()).throw(Error("registry unavailable"))
        url = self.serve(fake)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(get(url + "linux-64/repodata.json")[0], 502)
        self.assertIn("registry unavailable", stderr.getvalue())

    def test_listens_on_loopback_only(self):
        self.assertRegex(self.serve(registry()), r"^http://127\.0\.0\.1:\d+/$")

    def test_rattler_config(self):
        self.assertEqual(mirror.rattler_config("https://conda.example.org", "http://127.0.0.1:8/"),
                         '[mirrors]\n"https://conda.example.org/" = ["http://127.0.0.1:8/"]\n')
