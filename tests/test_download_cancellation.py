"""Cancellation during real HTTP transfers must stop before the body finishes."""

from __future__ import annotations

import hashlib
import http.server
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import pytest

from lean_runtime.errors import EnvironmentError
from lean_runtime.events import EventEmitter
from lean_runtime.locking import FileLock
from lean_runtime.oci import OCIRegistryClient, OCIRepository
from lean_runtime.store import EnvironmentStore


@pytest.mark.parametrize("sparse", [False, True])
def test_active_download_cancellation_and_retry(tmp_path: Path, sparse: bool) -> None:
    data = b"a" * (2 * 1024 * 1024)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    descriptor = {"digest": digest, "size": len(data)}
    sent = threading.Event()
    cancel = threading.Event()
    finish = threading.Event()
    requests: list[str | None] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested = self.headers.get("Range")
            requests.append(requested)
            start = int(requested.split("=")[1].split("-")[0]) if requested else 0
            body = data[start:]
            self.send_response(206 if requested else 200)
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                if len(requests) == 1:
                    # Less than a normal 1 MiB read: cancellation must not wait
                    # for read(n) to fill its entire buffer or reach EOF.
                    self.wfile.write(body[:32768])
                    self.wfile.flush()
                    sent.set()
                    cancel.wait(10)
                    self.wfile.write(body[32768:65536])
                    self.wfile.flush()
                    finish.wait(10)
                    self.wfile.write(body[65536:])
                else:
                    self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    store = EnvironmentStore(tmp_path / "consumer")
    client = OCIRegistryClient(
        OCIRepository.parse(f"oci+http://127.0.0.1:{server.server_port}/owner/cache")
    )

    def download() -> bytes:
        if sparse:
            return client.download_blob_range(
                descriptor, offset=0, size=len(data), expected_digest=digest, cancel=cancel
            )
        return client.download_blob(descriptor, store, EventEmitter(), cancel=cancel).read_bytes()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(download)
            try:
                assert sent.wait(5), "download never reached the HTTP body"
                cancel.set()
                with pytest.raises(EnvironmentError, match="cancelled"):
                    future.result(timeout=3)
                assert not (store.oci_blobs / digest.removeprefix("sha256:")).exists()
                # The cancelled call must release ownership so a retry can run.
                with FileLock(store.lock_paths.oci_blob(digest.removeprefix("sha256:")), timeout=0):
                    pass
            finally:
                cancel.set()
                finish.set()
        cancel.clear()
        assert download() == data
        assert len(requests) == 2
        if not sparse:
            assert not list(store.oci_blobs.glob("*.partial"))
    finally:
        cancel.set()
        finish.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
