"""Serve the channel from its registry on 127.0.0.1 for builds."""

import contextlib
import http.server
import shutil
import sys
import threading
import urllib.parse

from . import Error
from .naming import package_tag, repodata_tag, split_filename
from .registry import PACKAGE_V1, PACKAGE_V2, REPODATA, REPODATA_ZST, Registry, layer

REPODATA_FILES = {
    "repodata.json": (REPODATA, "application/json"),
    "repodata.json.zst": (REPODATA_ZST, "application/zstd"),
}
TITLE = "org.opencontainers.image.title"
CHUNK = 1 << 20


class Missing(Exception):
    """The channel has no such file."""


class ChannelFiles:
    """Files of the channel, read from the registry."""

    def __init__(self, registry: Registry, subdirs):
        self.registry = registry
        self.subdirs = frozenset(subdirs)
        self._repodata: dict[tuple[str, str], bytes] = {}
        self._lock = threading.Lock()

    def route(self, path: str) -> tuple[str, str]:
        segments = path.split("/")[1:]
        if len(segments) != 2:
            raise Missing
        subdir, filename = (urllib.parse.unquote(segment) for segment in segments)
        if subdir not in self.subdirs:
            raise Missing
        return subdir, filename

    def repodata(self, subdir: str, filename: str) -> bytes:
        """Repodata is read once per build, so every solve in the build sees the same packages."""
        key = (subdir, filename)
        with self._lock:
            if key not in self._repodata:
                manifest = self.registry.manifest(repodata_tag(subdir))
                found = manifest and layer(manifest, REPODATA_FILES[filename][0])
                if not found:
                    raise Missing
                self._repodata[key] = self.registry.blob(found["digest"])
            return self._repodata[key]

    def package(self, subdir: str, filename: str) -> dict:
        """The registry layer holding a package file."""
        if split_filename(filename) is None:
            raise Missing
        manifest = self.registry.manifest(package_tag(subdir, filename))
        media_type = PACKAGE_V2 if filename.endswith(".conda") else PACKAGE_V1
        found = manifest and layer(manifest, media_type)
        # Escaping is not one-to-one (`+` and `__p__` share a tag), so the layer must name the requested file.
        if not found or (found.get("annotations") or {}).get(TITLE) != filename:
            raise Missing
        return found


def _handler(channel: ChannelFiles):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self._serve(body=True)

        def do_HEAD(self):
            self._serve(body=False)

        def _serve(self, body: bool):
            try:
                subdir, filename = channel.route(urllib.parse.urlsplit(self.path).path)
                if filename in REPODATA_FILES:
                    data = channel.repodata(subdir, filename)
                    self._start(200, REPODATA_FILES[filename][1], len(data))
                    if body:
                        self.wfile.write(data)
                    return
                found = channel.package(subdir, filename)
                if not body:
                    self._start(200, "application/octet-stream", found.get("size"))
                    return
                stream = channel.registry.open_blob(found["digest"])
            except Missing:
                self._start(404, "text/plain", 0)
                return
            except (Error, ValueError) as err:
                print(f"channel mirror: {err}", file=sys.stderr, flush=True)
                self._start(502, "text/plain", 0)
                return
            with stream:
                self._start(200, "application/octet-stream", found.get("size"))
                try:
                    shutil.copyfileobj(stream, self.wfile, CHUNK)
                except Exception as err:
                    # The client sees a short body and retries; rattler checks each package's sha256.
                    print(f"channel mirror: {filename}: {err}", file=sys.stderr, flush=True)
                    self.close_connection = True

        def _start(self, status: int, content_type: str, length):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if isinstance(length, int):
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def log_message(self, format, *args):
            pass

    return Handler


@contextlib.contextmanager
def serve(registry: Registry, subdirs):
    """Serve the channel at the yielded http://127.0.0.1:<port>/ URL until the block ends."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(ChannelFiles(registry, subdirs)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def rattler_config(channel_url: str, mirror_url: str) -> str:
    """A rattler-build config.toml that fetches the channel from the mirror."""
    return f'[mirrors]\n"{channel_url.rstrip("/")}/" = ["{mirror_url}"]\n'
