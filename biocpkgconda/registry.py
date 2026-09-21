"""Read-only client for the OCI distribution API of GitHub Container Registry."""

import base64
import hashlib
import json
import os
import re
import threading
import urllib.parse

from . import Error, net

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
PACKAGE_V2 = "application/vnd.conda.package.v2"
PACKAGE_V1 = "application/vnd.conda.package.v1"
INDEX = "application/vnd.conda.info.index.v1+json"
REPODATA = "application/vnd.conda.repodata.v1+json"
REPODATA_ZST = "application/vnd.conda.repodata.v1+json+zst"
FAILURE = "application/vnd.biocpkgconda.failure.v1+json"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


class Registry:
    def __init__(self, host: str, repository: str, credentials: tuple[str, str] | None = None,
                 transport=net.request):
        self.host = host
        self.repository = repository
        self._credentials = credentials
        self._transport = transport
        self._token = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, host: str, repository: str, environ=os.environ) -> "Registry":
        """Authenticate as GITHUB_ACTOR with REGISTRY_TOKEN when both are set, else anonymously."""
        actor, token = environ.get("GITHUB_ACTOR"), environ.get("REGISTRY_TOKEN")
        return cls(host, repository, (actor, token) if actor and token else None)

    @property
    def reference(self) -> str:
        return f"{self.host}/{self.repository}"

    def _fetch_token(self) -> str:
        url = f"https://{self.host}/token?scope=repository:{self.repository}:pull&service={self.host}"
        secret = {}
        if self._credentials:
            basic = base64.b64encode(":".join(self._credentials).encode()).decode()
            secret["Authorization"] = f"Basic {basic}"
        response = self._transport(url, secret_headers=secret)
        if response.status != 200:
            raise Error(f"{self.host} refused a pull token for {self.repository} (HTTP {response.status})")
        try:
            body = json.loads(response.body)
            return body.get("token") or body["access_token"]
        except (ValueError, KeyError, AttributeError):
            raise Error(f"{self.host} returned a token response without a token") from None

    def _current_token(self, stale: str | None = None) -> str:
        with self._lock:
            if self._token is None or self._token == stale:
                self._token = self._fetch_token()
            return self._token

    def _get(self, url: str, accept: str | None = None) -> net.Response:
        if urllib.parse.urlsplit(url).netloc != self.host:
            raise Error(f"refusing to send registry credentials to {url}")
        headers = {"Accept": accept} if accept else {}
        token = self._current_token()
        response = self._transport(url, headers=headers, secret_headers={"Authorization": f"Bearer {token}"})
        # GHCR answers 403, not 401, once a request carries an expired or revoked token.
        if response.status in (401, 403):
            token = self._current_token(stale=token)
            response = self._transport(url, headers=headers, secret_headers={"Authorization": f"Bearer {token}"})
        return response

    def _url(self, path: str) -> str:
        return f"https://{self.host}/v2/{self.repository}/{path}"

    def list_tags(self) -> list[str]:
        """All tags, following pagination. A repository that does not exist yet has none."""
        tags: list[str] = []
        url = self._url("tags/list?n=1000")
        while url:
            response = self._get(url)
            if response.status == 404 and not tags:
                return []
            if response.status != 200:
                raise Error(f"listing tags of {self.reference} returned HTTP {response.status}")
            tags += json.loads(response.body).get("tags") or []
            link = _NEXT.search(response.headers.get("link", ""))
            url = urllib.parse.urljoin(url, link.group(1)) if link else None
        return tags

    def manifest(self, tag: str) -> dict | None:
        response = self._get(self._url(f"manifests/{tag}"), accept=MANIFEST)
        if response.status == 404:
            return None
        if response.status != 200:
            raise Error(f"manifest {self.reference}:{tag} returned HTTP {response.status}")
        return json.loads(response.body)

    def blob(self, digest: str) -> bytes:
        if not _DIGEST.fullmatch(digest):
            raise Error(f"unsupported digest {digest!r}")
        response = self._get(self._url(f"blobs/{digest}"))
        if response.status != 200:
            raise Error(f"blob {digest} of {self.reference} returned HTTP {response.status}")
        if "sha256:" + hashlib.sha256(response.body).hexdigest() != digest:
            raise Error(f"blob {digest} of {self.reference} does not match its digest")
        return response.body


def layer(manifest: dict, *media_types: str) -> dict | None:
    """The first layer with one of the given media types."""
    return next((item for item in manifest.get("layers", []) if item.get("mediaType") in media_types), None)
