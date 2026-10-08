import base64
import hashlib
import io
import json
import unittest
from unittest import mock

from biocpkgconda import Error, net
from biocpkgconda.net import Response
from biocpkgconda.registry import Registry, layer


class Transport:
    """Serves canned responses and records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.tokens = 0

    def __call__(self, url, headers=None, secret_headers=None, method="GET"):
        self.calls.append((url, headers or {}, secret_headers or {}))
        if url.startswith("https://ghcr.io/token"):
            self.tokens += 1
            return Response(200, {}, json.dumps({"token": f"t{self.tokens}"}).encode())
        route = self.routes[url]
        return route(secret_headers or {}) if callable(route) else route


def ok(body, headers=None):
    return Response(200, headers or {}, body if isinstance(body, bytes) else json.dumps(body).encode())


BASE = "https://ghcr.io/v2/example/channel"


class TokenTest(unittest.TestCase):
    def test_anonymous_token(self):
        transport = Transport({f"{BASE}/manifests/x": ok({"layers": []})})
        Registry("ghcr.io", "example/channel", transport=transport).manifest("x")
        url, _, secret = transport.calls[0]
        self.assertEqual(url, "https://ghcr.io/token?scope=repository:example/channel:pull&service=ghcr.io")
        self.assertEqual(secret, {})
        self.assertEqual(transport.calls[1][2], {"Authorization": "Bearer t1"})
        self.assertEqual(transport.calls[1][1], {"Accept": "application/vnd.oci.image.manifest.v1+json"})

    def test_credentials_from_environment(self):
        transport = Transport({f"{BASE}/manifests/x": ok({})})
        registry = Registry.from_environment("ghcr.io", "example/channel",
                                             {"GITHUB_ACTOR": "bot", "REGISTRY_TOKEN": "secret"})
        registry._transport = transport
        registry.manifest("x")
        self.assertEqual(transport.calls[0][2], {"Authorization": "Basic " + base64.b64encode(b"bot:secret").decode()})
        self.assertIsNone(Registry.from_environment("ghcr.io", "a/b", {"GITHUB_ACTOR": "bot"})._credentials)

    def test_refreshes_once_on_a_rejected_token(self):
        for status in (401, 403):
            with self.subTest(status=status):
                def manifest(secret, status=status):
                    if secret["Authorization"] == "Bearer t1":
                        return Response(status, {}, b"")
                    return ok({"schemaVersion": 2})

                transport = Transport({f"{BASE}/manifests/x": manifest})
                registry = Registry("ghcr.io", "example/channel", transport=transport)
                self.assertEqual(registry.manifest("x"), {"schemaVersion": 2})
                self.assertEqual(transport.tokens, 2)

    def test_denied(self):
        transport = Transport({f"{BASE}/manifests/x": Response(401, {}, b"")})
        with self.assertRaisesRegex(Error, "HTTP 401") as caught:
            Registry("ghcr.io", "example/channel", transport=transport).manifest("x")
        self.assertNotIn("t1", str(caught.exception))
        self.assertEqual(transport.tokens, 2)


class ApiTest(unittest.TestCase):
    def test_tags_follow_pagination(self):
        transport = Transport({
            f"{BASE}/tags/list?n=1000": ok({"tags": ["a", "b"]},
                                           {"link": '</v2/example/channel/tags/list?last=b&n=1000>; rel="next"'}),
            f"{BASE}/tags/list?last=b&n=1000": ok({"tags": ["c"]}),
        })
        self.assertEqual(Registry("ghcr.io", "example/channel", transport=transport).list_tags(), ["a", "b", "c"])

    def test_tags_of_missing_repository(self):
        transport = Transport({f"{BASE}/tags/list?n=1000": Response(404, {}, b"")})
        self.assertEqual(Registry("ghcr.io", "example/channel", transport=transport).list_tags(), [])

    def test_pagination_to_other_host(self):
        transport = Transport({f"{BASE}/tags/list?n=1000": ok({"tags": ["a"]},
                                                               {"link": '<https://evil.example/x>; rel="next"'})})
        with self.assertRaisesRegex(Error, "refusing"):
            Registry("ghcr.io", "example/channel", transport=transport).list_tags()

    def test_missing_manifest(self):
        transport = Transport({f"{BASE}/manifests/x": Response(404, {}, b"")})
        self.assertIsNone(Registry("ghcr.io", "example/channel", transport=transport).manifest("x"))

    def test_blob_digest_is_verified(self):
        data = b"{}"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        wrong = "sha256:" + "0" * 64
        transport = Transport({f"{BASE}/blobs/{digest}": ok(data), f"{BASE}/blobs/{wrong}": ok(data)})
        registry = Registry("ghcr.io", "example/channel", transport=transport)
        self.assertEqual(registry.blob(digest), data)
        with self.assertRaisesRegex(Error, "does not match"):
            registry.blob(wrong)
        with self.assertRaises(Error):
            registry.blob("md5:abc")

    def test_open_blob_refreshes_a_rejected_token(self):
        digest = "sha256:" + "a" * 64
        transport = Transport({})
        registry = Registry("ghcr.io", "example/channel", transport=transport)
        stream = io.BytesIO(b"data")
        with mock.patch.object(net, "open_stream", side_effect=[net.StatusError("denied", 403), stream]) as opened:
            self.assertIs(registry.open_blob(digest), stream)
        self.assertEqual([call.kwargs["secret_headers"] for call in opened.call_args_list],
                         [{"Authorization": "Bearer t1"}, {"Authorization": "Bearer t2"}])
        self.assertEqual(opened.call_args.args[0], f"{BASE}/blobs/{digest}")
        with mock.patch.object(net, "open_stream", side_effect=net.StatusError("missing", 404)):
            with self.assertRaisesRegex(Error, "missing"):
                registry.open_blob(digest)
        with self.assertRaises(Error):
            registry.open_blob("md5:abc")

    def test_layer(self):
        manifest = {"layers": [{"mediaType": "a", "digest": "1"}, {"mediaType": "b", "digest": "2"}]}
        self.assertEqual(layer(manifest, "b", "a")["digest"], "1")
        self.assertIsNone(layer(manifest, "c"))
        self.assertIsNone(layer({}, "a"))
