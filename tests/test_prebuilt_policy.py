from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from lean_runtime import (
    DEFAULT_ENVIRONMENT_LIBRARIES,
    DownloadUnavailable,
    EnvironmentError,
    EnvironmentLock,
    Runtime,
)
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


def test_public_cache_is_default_and_empty_environment_override_disables_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(home=tmp_path / "default")
    assert (
        tuple(cache.repository.display for cache in runtime.libraries)
        == DEFAULT_ENVIRONMENT_LIBRARIES
    )
    monkeypatch.setenv("LEAN_RUNTIME_LIBRARIES", "")
    assert Runtime(home=tmp_path / "disabled").libraries == ()


def test_auto_falls_back_only_when_prebuilt_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(home=tmp_path, libraries=[])
    runtime.libraries = (_Cache(DownloadUnavailable("cache miss")),)  # type: ignore[assignment]
    sentinel = object()
    monkeypatch.setattr(runtime.environments, "ensure", lambda *_args, **_kwargs: sentinel)
    assert runtime.open_exact(_lock()) is sentinel


def test_open_exact_forwards_environment_build_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(home=tmp_path, availability="local", libraries=[])
    captured: dict[str, object] = {}

    def ensure(*_args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runtime.environments, "ensure", ensure)
    runtime.open_exact(_lock(), build_timeout=3600)

    assert captured["build_timeout"] == 3600


def test_auto_does_not_hide_prebuilt_integrity_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(home=tmp_path, libraries=[])
    runtime.libraries = (_Cache(EnvironmentError("digest mismatch")),)  # type: ignore[assignment]
    source_build_called = False

    def source_build(*_args: object, **_kwargs: object) -> object:
        nonlocal source_build_called
        source_build_called = True
        return object()

    monkeypatch.setattr(runtime.environments, "ensure", source_build)
    with pytest.raises(EnvironmentError, match="digest mismatch"):
        runtime.open_exact(_lock())
    assert not source_build_called


def test_require_rejects_missing_cache_configuration(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path, availability="required", libraries=[])
    with pytest.raises(EnvironmentError, match="no environment libraries are configured"):
        runtime.open_exact(_lock())


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
