import io

import pytest

from lean_runtime.console import ConsoleRenderer
from lean_runtime.events import EventEmitter, RuntimeEvent
from lean_runtime.policies import format_byte_size, parse_byte_size


def _event(kind: str, message: str = "", **kwargs: object) -> RuntimeEvent:
    structured = {
        key: kwargs.pop(key) for key in ("phase", "current_bytes", "total_bytes") if key in kwargs
    }
    return RuntimeEvent(kind=kind, message=message, data=kwargs, **structured)  # type: ignore[arg-type]


def _renderer(
    mode: str, *, verbose: bool = False, clock=None
) -> tuple[ConsoleRenderer, io.StringIO]:
    stream = io.StringIO()
    renderer = ConsoleRenderer(
        stream,
        mode=mode,  # type: ignore[arg-type]
        verbose=verbose,
        clock=clock if clock is not None else lambda: 0.0,
    )
    return renderer, stream


def test_byte_size_round_trip() -> None:
    assert parse_byte_size("500MiB") == 500 * 2**20
    assert parse_byte_size("1.5 GB") == 1_500_000_000
    assert parse_byte_size("1048576") == 1048576
    assert format_byte_size(0) == "0 B"
    assert format_byte_size(228 * 2**20) == "228 MiB"
    assert format_byte_size(2 * 2**30 + 2**29) == "2.5 GiB"
    with pytest.raises(ValueError):
        parse_byte_size("many")
    with pytest.raises(ValueError):
        parse_byte_size("12 lightyears")


def test_warm_run_prints_nothing() -> None:
    renderer, stream = _renderer("plain")
    for kind in (
        "environment.ensure_started",
        "environment.cache_hit",
        "environment.ready",
        "library.lookup",
        "source.cache_hit",
        "resolution.completed",
    ):
        renderer(_event(kind, "internal"))
    renderer.close()
    assert stream.getvalue() == ""


def test_quiet_mode_prints_nothing_ever() -> None:
    renderer, stream = _renderer("quiet")
    renderer(_event("acquisition.planned", download_bytes=100, cached_bytes=0))
    renderer.note("Discovering an exact environment")
    assert stream.getvalue() == ""


def test_plain_mode_prints_plan_and_checkpoints() -> None:
    renderer, stream = _renderer("plain")
    renderer(
        _event(
            "acquisition.planned",
            download_bytes=1000,
            cached_bytes=24,
            total_bytes=1024,
        )
    )
    renderer(
        _event("library.layer_progress", digest="sha256:a", current_bytes=300, total_bytes=600)
    )
    renderer(
        _event("library.layer_progress", digest="sha256:a", current_bytes=600, total_bytes=600)
    )
    renderer(
        _event("library.layer_progress", digest="sha256:b", current_bytes=400, total_bytes=400)
    )
    renderer(_event("library.verified", "done"))
    lines = stream.getvalue().splitlines()
    assert lines == [
        "Downloading environment: 1000 B (24 B already cached)",
        "Downloaded 25%",
        "Downloaded 50%",
        "Downloaded 75%",
        "Downloaded 100%",
        "Downloaded and verified environment",
    ]


def test_plan_with_nothing_to_download_is_silent() -> None:
    renderer, stream = _renderer("plain")
    renderer(_event("acquisition.planned", download_bytes=0, cached_bytes=1024))
    renderer(_event("library.verified", "done"))
    assert stream.getvalue() == ""


def test_tty_mode_redraws_one_line_and_finishes_it() -> None:
    time = [0.0]
    renderer, stream = _renderer("tty", clock=lambda: time[0])
    renderer(_event("acquisition.planned", download_bytes=200, cached_bytes=0))
    renderer(
        _event("library.layer_progress", digest="sha256:a", current_bytes=100, total_bytes=200)
    )
    time[0] = 0.01  # within redraw throttle: dropped
    renderer(
        _event("library.layer_progress", digest="sha256:a", current_bytes=150, total_bytes=200)
    )
    time[0] = 1.0
    renderer(
        _event("library.layer_progress", digest="sha256:a", current_bytes=200, total_bytes=200)
    )
    renderer(_event("library.verified", "done"))
    output = stream.getvalue()
    assert output.count("\r") == 3  # 50%, 100%, and the completion redraw
    assert "150 B" not in output
    assert "100% · 200 B/200 B" in output
    assert output.endswith("Downloaded and verified environment\n")


def test_retry_and_resume_are_visible() -> None:
    renderer, stream = _renderer("plain")
    renderer(_event("library.layer_download_started", resumed_bytes=2048, size=4096))
    renderer(_event("library.layer_download_retry", "retrying"))
    lines = stream.getvalue().splitlines()
    assert lines == [
        "Resuming interrupted download from 2 KiB",
        "Retrying download after integrity verification failed",
    ]


def test_cold_context_lines() -> None:
    renderer, stream = _renderer("plain")
    renderer(_event("package_reference.started", reference="mathlib@v4.33.0"))
    renderer(
        _event(
            "discovery.candidate_started",
            candidate="mathlib-v4.33.0",
            toolchain="leanprover/lean4:v4.33.0",
        )
    )
    renderer(_event("toolchain.install_started", toolchain="leanprover/lean4:v4.33.0"))
    renderer(_event("source.fetch_started", package="mathlib"))
    renderer(_event("artifact.hydration_started", package="mathlib"))
    renderer(
        _event(
            "availability.fallback",
            library="oci://ghcr.io/owner/cache",
            reason_code="remote_candidate_missing",
        )
    )
    renderer(_event("environment.build_started", "building"))
    lines = stream.getvalue().splitlines()
    assert lines == [
        "Resolving mathlib@v4.33.0",
        "Trying mathlib-v4.33.0 · leanprover/lean4:v4.33.0",
        "Installing Lean toolchain leanprover/lean4:v4.33.0 (one-time, can take minutes)",
        "Fetching mathlib source",
        "Hydrating build artifacts for mathlib",
        "WARNING: downloadable environment unavailable from oci://ghcr.io/owner/cache "
        "(remote_candidate_missing)",
        "Building environment from source (large dependencies such as Mathlib can take "
        "30+ minutes; pass --no-source-build to fail fast instead)",
    ]


def test_verbose_prints_every_event_with_bytes() -> None:
    renderer, stream = _renderer("plain", verbose=True)
    renderer(
        _event("environment.cache_hit", "Reusing published environment", environment_id="env_1")
    )
    renderer(
        _event(
            "library.layer_progress",
            "Downloading OCI blob",
            digest="sha256:a",
            current_bytes=512,
            total_bytes=1024,
        )
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "environment.cache_hit: Reusing published environment (environment_id=env_1)"
    assert (
        lines[1] == "library.layer_progress: Downloading OCI blob [512 B/1 KiB] (digest=sha256:a)"
    )


def test_event_emitter_carries_structured_progress_fields() -> None:
    events: list[RuntimeEvent] = []
    EventEmitter(events.append).emit(
        "library.layer_progress",
        "Downloading",
        phase="download",
        current_bytes=1,
        total_bytes=2,
        digest="sha256:a",
    )
    event = events[0]
    assert event.phase == "download"
    assert event.current_bytes == 1
    assert event.total_bytes == 2
    assert event.data == {"digest": "sha256:a"}
    assert event.to_dict()["current_bytes"] == 1
    plain = RuntimeEvent(kind="x", message="y").to_dict()
    assert "current_bytes" not in plain and "phase" not in plain
