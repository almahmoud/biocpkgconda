import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from biocpkgconda import Error, net


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"ok", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def http_error(code):
    return urllib.error.HTTPError("https://example.org", code, "error", {}, io.BytesIO(b"denied"))


@mock.patch.object(net, "sleep", lambda seconds: None)
class RequestTest(unittest.TestCase):
    def test_retries_server_errors(self):
        with mock.patch.object(net.urllib.request, "urlopen",
                               side_effect=[http_error(503), urllib.error.URLError("reset"),
                                            FakeResponse(b"body", headers={"Content-Type": "text/plain"})]) as urlopen:
            response = net.request("https://example.org/x")
        self.assertEqual((response.status, response.body), (200, b"body"))
        self.assertEqual(response.headers, {"content-type": "text/plain"})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(urlopen.call_args.args[0].get_header("User-agent"), net.USER_AGENT)

    def test_client_errors_are_returned(self):
        with mock.patch.object(net.urllib.request, "urlopen", side_effect=[http_error(404)]):
            response = net.request("https://example.org/x")
        self.assertEqual((response.status, response.body), (404, b"denied"))

    def test_gives_up(self):
        errors = [http_error(502) for _ in range(net.RETRIES + 1)]
        with mock.patch.object(net.urllib.request, "urlopen", side_effect=errors) as urlopen:
            with self.assertRaisesRegex(Error, "failed after 4 attempts: HTTP 502"):
                net.request("https://example.org/x")
        self.assertEqual(urlopen.call_count, net.RETRIES + 1)

    def test_secret_headers_are_not_redirected(self):
        with mock.patch.object(net.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            net.request("https://example.org/x", secret_headers={"Authorization": "Bearer t"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.unredirected_hdrs, {"Authorization": "Bearer t"})
        self.assertNotIn("Authorization", request.headers)

    def test_open_stream(self):
        with mock.patch.object(net.urllib.request, "urlopen", side_effect=[http_error(500), FakeResponse(b"data")]):
            with net.open_stream("https://example.org/x") as stream:
                self.assertEqual(stream.read(), b"data")
        with mock.patch.object(net.urllib.request, "urlopen", side_effect=[http_error(404)]):
            with self.assertRaisesRegex(Error, "HTTP 404"):
                net.open_stream("https://example.org/x")


class FetchTest(unittest.TestCase):
    def test_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(net, "request", return_value=net.Response(200, {}, b"PACKAGES")) as request:
                self.assertEqual(net.fetch("https://example.org/src/contrib/PACKAGES", Path(tmp)), b"PACKAGES")
                self.assertEqual(net.fetch("https://example.org/src/contrib/PACKAGES", Path(tmp)), b"PACKAGES")
            self.assertEqual(request.call_count, 1)
            self.assertEqual([p.name.split("-", 1)[1] for p in Path(tmp).iterdir()], ["PACKAGES"])

    def test_non_200_fails(self):
        with mock.patch.object(net, "request", return_value=net.Response(404, {}, b"")):
            with self.assertRaisesRegex(Error, "HTTP 404"):
                net.fetch("https://example.org/missing")
