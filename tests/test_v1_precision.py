from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import EnvironmentLock, LockedPackage, MatrixContext, Runtime
from lean_runtime.comparison import compare_locks
from lean_runtime.facade import check_matrix_async
from lean_runtime.matrix import load_matrix, run_matrix
from lean_runtime.models import ExecutionResult
from lean_runtime.profiling import run_profile
from lean_runtime.runtime import _download_reason
from lean_runtime.serialization import write_json_atomic
from lean_runtime.store import environment_identity, platform_compatibility, platform_record
from lean_runtime.verification import load_lock_subject
from lean_runtime.wire import serialize_execution_v1, serialize_verify_v1


def test_removed_v1_runtime_names_are_absent() -> None:
    removed = {
        "resolve",
        "ensure",
        "open",
        "resolve_references",
        "ensure_references",
        "export_environment",
        "import_environment",
        "publish_environment_index",
        "gc",
        "gc_oci_blobs",
        "open_program",
        "export_program",
        "import_program",
        "pull_program",
        "publish_program_index",
    }
    assert not {name for name in removed if hasattr(Runtime, name)}


def lock(*, toolchain: str = "leanprover/lean4:v4.32.0", revision: str = "a" * 40):
    return EnvironmentLock(
        toolchain=toolchain,
        spec_digest="spec_" + "c" * 64,
        root_lakefile='name = "test"\n',
        root_module="/- root -/\n",
        manifest={"version": "1.1.0", "packages": []},
        packages=(
            LockedPackage(
                name="sample",
                url="https://example.test/sample",
                revision=revision,
                source_id="source_" + "d" * 64,
                tree_hash="b" * 40,
            ),
        ),
    )


def result(*, ok: bool = True, elapsed: float = 0.01) -> ExecutionResult:
    return ExecutionResult(
        ok=ok,
        exit_code=0 if ok else 1,
        toolchain="leanprover/lean4:v4.32.0",
        command=("lean", "Main.lean"),
        cwd="/tmp",
        stdout="",
        stderr="",
        elapsed_seconds=elapsed,
    )


def test_lock_verification_uses_canonical_loader_and_v1_envelope(tmp_path: Path) -> None:
    path = tmp_path / "environment.lock.json"
    lock().write(path)
    report = load_lock_subject(path)
    payload = serialize_verify_v1(report)
    assert report.ok
    assert payload["schema"] == "lean-runtime.verify/v1"
    assert payload["data"]["environment_id"] is None
    assert any(check["skipped"] for check in payload["data"]["checks"])


def test_diff_is_order_independent_and_distinguishes_same_tree_new_commit() -> None:
    same = EnvironmentLock(
        toolchain=lock().toolchain,
        spec_digest=lock().spec_digest,
        root_lakefile=lock().root_lakefile,
        root_module=lock().root_module,
        manifest=lock().manifest,
        packages=tuple(reversed(lock().packages)),
    )
    assert compare_locks(lock(), same).equal
    changed = compare_locks(lock(), lock(revision="e" * 40))
    assert [item.path for item in changed.changes] == ["packages.sample.revision"]
    assert changed.changes[0].identity_effect


def test_profile_excludes_warmups_and_stops_on_failure() -> None:
    values = iter((result(), result(elapsed=0.02), result(ok=False, elapsed=0.03), result()))
    environment = SimpleNamespace(check=lambda *_args, **_kwargs: next(values))
    report = run_profile(environment, "source", filename="Main.lean", warmup=1, repeat=3)
    assert len(report.results) == 2
    assert report.durations_ms == (20, 30)
    assert not report.ok
    assert report.statistics()["p95"] is None


def test_matrix_parser_is_closed_and_execution_preserves_context_order(tmp_path: Path) -> None:
    configuration = tmp_path / "matrix.toml"
    configuration.write_text(
        '[[context]]\nname = "first"\ntoolchain = "4.32.0"\n\n'
        '[[context]]\nname = "second"\nenvironment = "ready"\n'
    )
    contexts = load_matrix(configuration)
    environment = SimpleNamespace(check=lambda *_args, **_kwargs: result())
    runtime = SimpleNamespace(
        check=lambda *_args, **_kwargs: result(),
        environment=lambda _name: environment,
    )
    report = run_matrix(
        runtime,
        "source",
        filename="Main.lean",
        contexts=contexts,
        base=tmp_path,
        concurrency=2,
    )
    assert [entry.context for entry in report.entries] == ["first", "second"]
    assert report.ok

    configuration.write_text('[[context]]\nname="bad"\ntoolchain="4.32.0"\nextra=true\n')
    with pytest.raises(Exception, match="unknown fields"):
        load_matrix(configuration)


def test_matrix_passes_one_cancellation_signal_to_every_active_check(tmp_path: Path) -> None:
    observed: list[threading.Event | None] = []

    def check(*_args, **kwargs):
        observed.append(kwargs.get("cancel"))
        return result()

    cancel = threading.Event()
    runtime = SimpleNamespace(check=check)
    contexts = (
        MatrixContext("one", toolchain="4.32.0"),
        MatrixContext("two", toolchain="4.32.0"),
    )
    run_matrix(
        runtime,
        "source",
        filename="Main.lean",
        contexts=contexts,
        base=tmp_path,
        concurrency=2,
        cancel=cancel,
    )
    assert observed == [cancel, cancel]


def test_async_matrix_cancellation_waits_for_active_checks_to_stop() -> None:
    stopped = threading.Event()

    def check(*_args, **kwargs):
        cancel = kwargs["cancel"]
        assert cancel is not None
        cancel.wait(5)
        stopped.set()
        return result(ok=False)

    runtime = SimpleNamespace(
        check_matrix=lambda source, **kwargs: run_matrix(
            SimpleNamespace(check=check),
            source,
            filename=kwargs["filename"],
            contexts=tuple(kwargs["contexts"]),
            base=Path("."),
            concurrency=kwargs["concurrency"],
            cancel=kwargs["cancel"],
        )
    )

    async def cancel_matrix() -> None:
        task = asyncio.create_task(
            check_matrix_async(
                "source",
                contexts=(MatrixContext("one", toolchain="4.32.0"),),
                runtime=runtime,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_matrix())
    assert stopped.is_set()


def test_execution_serializer_has_stable_envelope() -> None:
    payload = serialize_execution_v1(result())
    assert json.loads(json.dumps(payload))["schema"] == "lean-runtime.execution/v1"
    assert set(payload) == {"schema", "ok", "data", "warnings", "errors"}


def test_verify_distinguishes_platform_mismatch_before_open(tmp_path: Path) -> None:
    runtime = Runtime(home=tmp_path, availability="local")
    selected = lock()
    runtime.store.publish_lock(selected)
    environment_id = environment_identity(selected)
    root = runtime.store.environment_path(environment_id)
    root.mkdir()
    compatibility = {**platform_compatibility(), "machine": "incompatible-machine"}
    write_json_atomic(
        root / "metadata.json",
        {
            "schema": "lean-runtime-published-environment/1",
            "environment_id": environment_id,
            "lock_id": selected.lock_id,
            "toolchain": selected.toolchain,
            "platform": platform_record(),
            "platform_compatibility": compatibility,
            "build_profile": "release",
            "status": "ready",
            "created_at": "2026-08-04T00:00:00+00:00",
        },
    )
    report = runtime.verify(environment_id)
    assert not report.ok
    assert report.failures[0].code == "platform_compatibility_mismatch"
    assert any(item.skipped for item in report.checks)


def test_matrix_context_is_public() -> None:
    assert MatrixContext("core", toolchain="4.32.0").name == "core"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("platform compatibility does not match", "platform_compatibility_mismatch"),
        ("signature was missing", "signature_policy_rejected"),
        ("manifest digest mismatch", "remote_candidate_corrupt"),
        ("manifest not found", "remote_candidate_missing"),
    ],
)
def test_prebuilt_failures_have_stable_reason_codes(message: str, code: str) -> None:
    assert _download_reason(RuntimeError(message)) == code
