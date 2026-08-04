from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, EnvironmentLock, PrebuiltUnavailable, Runtime
from lean_runtime.oci import _SafeRedirectHandler


def _lock() -> EnvironmentLock:
    return EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_" + "a" * 64,
        root_lakefile='name = "test"\n',
        root_module="/- test -/\n",
        manifest={"version": "1.1.0", "packages": []},
        packages=(),
    )


class _Repository:
    display = "oci://registry.example/owner/cache"


class _Cache:
    repository = _Repository()

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def pull(self, *_args: object, **_kwargs: object) -> str:
        raise self.failure


def test_auto_falls_back_only_when_prebuilt_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(home=tmp_path, caches=[])
    runtime.caches = (_Cache(PrebuiltUnavailable("cache miss")),)  # type: ignore[assignment]
    sentinel = object()
    monkeypatch.setattr(runtime.environments, "ensure", lambda *_args, **_kwargs: sentinel)
    assert runtime.ensure(_lock()) is sentinel


def test_auto_does_not_hide_prebuilt_integrity_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(home=tmp_path, caches=[])
    runtime.caches = (_Cache(EnvironmentError("digest mismatch")),)  # type: ignore[assignment]
    source_build_called = False

    def source_build(*_args: object, **_kwargs: object) -> object:
        nonlocal source_build_called
        source_build_called = True
        return object()

    monkeypatch.setattr(runtime.environments, "ensure", source_build)
    with pytest.raises(EnvironmentError, match="digest mismatch"):
        runtime.ensure(_lock())
    assert not source_build_called


def test_require_rejects_missing_cache_configuration(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path, prebuilt="require", caches=[])
    with pytest.raises(EnvironmentError, match="no caches configured"):
        runtime.ensure(_lock())


def test_registry_redirect_does_not_forward_authorization_cross_host() -> None:
    request = urllib.request.Request("https://registry.example/v2/owner/cache/blobs/sha256:x")
    request.add_header("Authorization", "Bearer secret")
    redirected = _SafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://storage.example/blob",
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None
