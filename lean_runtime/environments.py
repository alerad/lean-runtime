"""Published environments and reproducible executions within them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generic, TypeVar

from .backends import Backend, BackendResult
from .diagnostics import error_diagnostic, parse_diagnostics
from .errors import EnvironmentError, MaterializationError
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock
from .locking import FileLock
from .models import (
    ExecutionProvenance,
    ExecutionResult,
    PackageProvenance,
)
from .policies import ExecutionPolicy
from .serialization import sha256_id, sha256_text, write_json_atomic
from .store import EnvironmentStore, clone_tree, environment_identity, platform_record
from .toolchains import ToolchainManager

ENVIRONMENT_SCHEMA = "lean-runtime-published-environment/1"
EXECUTION_SCHEMA = "lean-runtime-execution/1"
CAPTURE_SCHEMA = "lean-runtime-execution-capture/1"
T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class EnvironmentInfo:
    environment_id: str
    lock_id: str
    toolchain: str
    packages: int
    platform: dict[str, str]
    build_profile: str
    status: str
    created_at: str
    names: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionCapture:
    environment_id: str
    lock: EnvironmentLock
    operation: str
    files: dict[str, str]
    policy: ExecutionPolicy
    expected_ok: bool | None = None

    @property
    def capture_id(self) -> str:
        return sha256_id("capture", self.to_dict(include_id=False))

    @property
    def lock_id(self) -> str:
        return self.lock.lock_id

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        value = {
            "schema": CAPTURE_SCHEMA,
            "environment_id": self.environment_id,
            "lock": self.lock.to_dict(),
            "operation": self.operation,
            "files": dict(sorted(self.files.items())),
            "policy": self.policy.to_dict(),
            "expected_ok": self.expected_ok,
        }
        return {"capture_id": self.capture_id, **value} if include_id else value

    def write(self, path: str | os.PathLike[str]) -> None:
        write_json_atomic(Path(path), self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExecutionCapture:
        if value.get("schema") != CAPTURE_SCHEMA:
            raise EnvironmentError(f"unsupported capture schema: {value.get('schema')!r}")
        files = value.get("files")
        if not isinstance(files, dict) or not files:
            raise EnvironmentError("execution capture files must be a non-empty object")
        normalized_files: dict[str, str] = {}
        for name, source in files.items():
            if not isinstance(name, str) or Path(name).name != name or not isinstance(source, str):
                raise EnvironmentError("execution capture contains an unsafe file entry")
            normalized_files[name] = source
        policy_value = value.get("policy")
        if not isinstance(policy_value, dict):
            raise EnvironmentError("execution capture policy must be an object")
        expected_ok = value.get("expected_ok")
        if expected_ok is not None and not isinstance(expected_ok, bool):
            raise EnvironmentError("execution capture expected_ok must be Boolean or null")
        capture = cls(
            environment_id=str(value["environment_id"]),
            lock=EnvironmentLock.from_dict(dict(value["lock"])),
            operation=str(value["operation"]),
            files=normalized_files,
            policy=ExecutionPolicy(**policy_value),
            expected_ok=expected_ok,
        )
        recorded = value.get("capture_id")
        if recorded is not None and recorded != capture.capture_id:
            raise EnvironmentError("execution capture identity mismatch")
        return capture

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ExecutionCapture:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise EnvironmentError("execution capture must contain an object")
        return cls.from_dict(value)


class ExecutionJob(Generic[T]):
    """A cancellable background execution."""

    def __init__(self, function: Any) -> None:
        self._cancel = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lean-runtime")
        self._future: Future[T] = self._executor.submit(function, self._cancel)

    def cancel(self) -> bool:
        self._cancel.set()
        return True

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: float | None = None) -> T:
        try:
            return self._future.result(timeout=timeout)
        finally:
            if self._future.done():
                self._executor.shutdown(wait=False)


class Environment:
    """A lightweight handle to one published, content-addressed environment."""

    def __init__(
        self,
        manager: EnvironmentManager,
        environment_id: str,
        lock: EnvironmentLock,
        root: Path,
        record: dict[str, Any],
    ) -> None:
        self.manager = manager
        self.id = environment_id
        self.lock = lock
        self.root = root
        self.workspace = root / "workspace"
        self._record = record

    def inspect(self) -> EnvironmentInfo:
        names = tuple(
            sorted(name for name, value in self.manager.store.aliases().items() if value == self.id)
        )
        return EnvironmentInfo(
            environment_id=self.id,
            lock_id=self.lock.lock_id,
            toolchain=self.lock.toolchain,
            packages=len(self.lock.packages),
            platform=dict(self._record["platform"]),
            build_profile=str(self._record["build_profile"]),
            status=str(self._record["status"]),
            created_at=str(self._record["created_at"]),
            names=names,
        )

    def check(
        self,
        source: str,
        *,
        filename: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        selected_policy = policy or ExecutionPolicy()
        safe_filename = Path(filename).name
        if not safe_filename.endswith(".lean"):
            safe_filename += ".lean"
        return self._execute_in_instance(
            operation="check",
            source=source,
            filename=safe_filename,
            targets=(),
            policy=selected_policy,
            cancel=cancel,
        )

    def start_check(
        self,
        source: str,
        *,
        filename: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
    ) -> ExecutionJob[ExecutionResult]:
        return ExecutionJob(
            lambda cancel: self.check(source, filename=filename, policy=policy, cancel=cancel)
        )

    def check_many(
        self,
        sources: Sequence[str],
        *,
        concurrency: int = 4,
        policy: ExecutionPolicy | None = None,
    ) -> tuple[ExecutionResult, ...]:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(self.check, source, policy=policy) for source in sources]
            return tuple(future.result() for future in futures)

    def build(
        self,
        targets: Sequence[str] = (),
        *,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        return self._execute_in_instance(
            operation="build",
            source="",
            filename="",
            targets=tuple(targets),
            policy=policy or ExecutionPolicy(timeout_seconds=900, max_output_bytes=10_000_000),
            cancel=cancel,
        )

    def capture(
        self,
        source: str,
        *,
        filename: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
        expected_ok: bool | None = None,
    ) -> ExecutionCapture:
        safe_filename = Path(filename).name
        if not safe_filename.endswith(".lean"):
            safe_filename += ".lean"
        return ExecutionCapture(
            environment_id=self.id,
            lock=self.lock,
            operation="check",
            files={safe_filename: source},
            policy=policy or ExecutionPolicy(),
            expected_ok=expected_ok,
        )

    def _execute_in_instance(
        self,
        *,
        operation: str,
        source: str,
        filename: str,
        targets: tuple[str, ...],
        policy: ExecutionPolicy,
        cancel: threading.Event | None,
    ) -> ExecutionResult:
        source_digest = sha256_text(source)
        execution_id = sha256_id(
            "execution",
            {
                "schema": EXECUTION_SCHEMA,
                "environment_id": self.id,
                "operation": operation,
                "source_digest": source_digest,
                "filename": filename,
                "targets": list(targets),
                "policy": policy.to_dict(),
                "backend": self.manager.backend.name,
            },
        )
        started_at = _now()
        job_parent = self.manager.store.jobs / execution_id
        job_parent.mkdir(parents=True, exist_ok=True)
        instance = job_parent / f"instance-{uuid.uuid4().hex}"
        try:
            clone_tree(self.workspace, instance)
            if operation == "check":
                source_path = instance / filename
                source_path.write_text(source, encoding="utf-8")
                command = self.manager.toolchains.command(
                    self.lock.toolchain, "lake", "env", "lean", str(source_path)
                )
            else:
                command = self.manager.toolchains.command(
                    self.lock.toolchain, "lake", "build", *targets
                )
            raw = self.manager.backend.execute(
                command,
                cwd=instance,
                environment=self.manager.toolchains.environment,
                policy=policy,
                cancel=cancel,
            )
            result = self._result(
                raw,
                command=command,
                cwd=instance,
                execution_id=execution_id,
                source_digest=source_digest,
                started_at=started_at,
                policy=policy,
            )
            self._record_execution(result, operation, targets)
            return result
        finally:
            if instance.exists():
                shutil.rmtree(instance)
            with suppress(OSError):
                job_parent.rmdir()

    def _result(
        self,
        raw: BackendResult,
        *,
        command: Sequence[str],
        cwd: Path,
        execution_id: str,
        source_digest: str,
        started_at: str,
        policy: ExecutionPolicy,
    ) -> ExecutionResult:
        combined = "\n".join(part for part in (raw.stdout, raw.stderr) if part)
        diagnostics = parse_diagnostics(combined)
        if raw.timed_out:
            diagnostics += (error_diagnostic("Lean execution exceeded its time limit"),)
        if raw.cancelled:
            diagnostics += (error_diagnostic("Lean execution was cancelled"),)
        provenance = ExecutionProvenance(
            environment_id=self.id,
            execution_id=execution_id,
            lock_id=self.lock.lock_id,
            toolchain=self.lock.toolchain,
            packages=tuple(
                PackageProvenance(package.name, package.url, package.revision, package.tree_hash)
                for package in self.lock.packages
            ),
            platform=platform_record(),
            backend=self.manager.backend.name,
            requested_policy=policy.to_dict(),
            enforced_policy_fields=raw.enforced_policy_fields,
            source_digest=source_digest,
            started_at=started_at,
        )
        return ExecutionResult(
            ok=raw.exit_code == 0,
            exit_code=raw.exit_code,
            toolchain=self.lock.toolchain,
            command=tuple(command),
            cwd=str(cwd),
            stdout=raw.stdout,
            stderr=raw.stderr,
            elapsed_seconds=raw.elapsed_seconds,
            timed_out=raw.timed_out,
            cancelled=raw.cancelled,
            output_truncated=raw.output_truncated,
            diagnostics=diagnostics,
            provenance=provenance,
        )

    def _record_execution(
        self, result: ExecutionResult, operation: str, targets: tuple[str, ...]
    ) -> None:
        assert result.execution_id is not None
        write_json_atomic(
            self.manager.store.executions / f"{result.execution_id}.json",
            {
                "schema": EXECUTION_SCHEMA,
                "operation": operation,
                "targets": list(targets),
                "result": result.to_dict(),
            },
        )


class EnvironmentManager:
    def __init__(
        self,
        store: EnvironmentStore,
        toolchains: ToolchainManager,
        backend: Backend,
    ) -> None:
        self.store = store
        self.toolchains = toolchains
        self.backend = backend

    def ensure(
        self,
        lock: EnvironmentLock,
        *,
        name: str | None = None,
        build_profile: str = "release",
    ) -> Environment:
        self.store.publish_lock(lock)
        environment_id = environment_identity(lock, build_profile)
        destination = self.store.environment_path(environment_id)
        with FileLock(self.store.lock_dir / f"{environment_id}.lock", timeout=1800):
            if not destination.is_dir():
                self._ensure_sources(lock)
                self._materialize(lock, environment_id, destination, build_profile)
        environment = self.open(environment_id)
        if name:
            self.store.set_alias(name, environment_id)
        return environment

    def open(self, identifier: str) -> Environment:
        environment_id = self.store.resolve_identifier(identifier)
        root = self.store.environment_path(environment_id)
        record_path = root / "metadata.json"
        if not record_path.is_file():
            raise EnvironmentError(f"published environment has no metadata: {environment_id}")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("schema") != ENVIRONMENT_SCHEMA or record.get("status") != "ready":
            raise EnvironmentError(f"environment is not ready: {environment_id}")
        lock = self.store.load_lock(str(record["lock_id"]))
        expected = environment_identity(lock, str(record["build_profile"]))
        if expected != environment_id or record.get("environment_id") != environment_id:
            raise EnvironmentError(f"environment identity mismatch: {environment_id}")
        if not (root / "workspace" / ".lake" / "build").is_dir():
            raise EnvironmentError(f"environment build artifacts are missing: {environment_id}")
        return Environment(self, environment_id, lock, root, record)

    def _materialize(
        self,
        lock: EnvironmentLock,
        environment_id: str,
        destination: Path,
        build_profile: str,
    ) -> None:
        stage = self.store.environments / f".staging-{os.getpid()}-{uuid.uuid4().hex}"
        workspace = stage / "workspace"
        try:
            workspace.mkdir(parents=True)
            (workspace / "lean-toolchain").write_text(lock.toolchain + "\n", encoding="utf-8")
            (workspace / "lakefile.toml").write_text(lock.root_lakefile, encoding="utf-8")
            (workspace / f"{ROOT_MODULE}.lean").write_text(lock.root_module, encoding="utf-8")
            write_json_atomic(workspace / "lake-manifest.json", lock.manifest)
            packages_dir = workspace / str(lock.manifest.get("packagesDir", ".lake/packages"))
            packages_dir.mkdir(parents=True, exist_ok=True)
            for package in lock.packages:
                source = self.store.source_path(package.source_id)
                if not source.is_dir():
                    raise MaterializationError(
                        f"locked package source is unavailable: {package.name}",
                        phase="acquisition",
                    )
                clone_tree(source, packages_dir / package.name)
            build_policy = ExecutionPolicy(timeout_seconds=1800, max_output_bytes=10_000_000)
            hydration: list[dict[str, Any]] = []
            for package in lock.packages:
                if not package.artifact_command:
                    continue
                command = list(package.artifact_command)
                if command[0] in {"lake", "lean"}:
                    command = self.toolchains.command(lock.toolchain, command[0], *command[1:])
                result = self.backend.execute(
                    command,
                    cwd=workspace,
                    environment=self.toolchains.environment,
                    policy=build_policy,
                )
                hydration.append(
                    {
                        "package": package.name,
                        "command": command,
                        "exit_code": result.exit_code,
                        "output": result.stdout + result.stderr,
                    }
                )
                if result.exit_code:
                    raise MaterializationError(
                        f"artifact hydration failed for {package.name}",
                        phase="artifact-hydration",
                        command=tuple(command),
                        exit_code=result.exit_code,
                        output=result.stdout + result.stderr,
                    )
            command = self.toolchains.command(lock.toolchain, "lake", "build")
            result = self.backend.execute(
                command,
                cwd=workspace,
                environment=self.toolchains.environment,
                policy=build_policy,
            )
            if result.exit_code:
                raise MaterializationError(
                    "environment build failed",
                    phase="build",
                    command=tuple(command),
                    exit_code=result.exit_code,
                    output=result.stdout + result.stderr,
                )
            metadata = {
                "schema": ENVIRONMENT_SCHEMA,
                "environment_id": environment_id,
                "lock_id": lock.lock_id,
                "toolchain": lock.toolchain,
                "platform": platform_record(),
                "build_profile": build_profile,
                "status": "ready",
                "created_at": _now(),
                "hydration": hydration,
                "build": {
                    "command": command,
                    "exit_code": result.exit_code,
                    "elapsed_seconds": result.elapsed_seconds,
                    "output_truncated": result.output_truncated,
                },
            }
            write_json_atomic(stage / "metadata.json", metadata)
            stage.replace(destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def _ensure_sources(self, lock: EnvironmentLock) -> None:
        """Acquire exact locked sources without invoking Lake resolution."""
        acquisition_root = self.store.home / "acquisition"
        acquisition_root.mkdir(parents=True, exist_ok=True)
        for package in lock.packages:
            if self.store.source_path(package.source_id).is_dir():
                self.store.validate_source(
                    package.source_id,
                    url=package.url,
                    revision=package.revision,
                    tree_hash=package.tree_hash,
                )
                continue
            with tempfile.TemporaryDirectory(
                prefix=f"{package.name}-", dir=acquisition_root
            ) as raw:
                checkout = Path(raw)
                commands = [
                    ["git", "init", "-q"],
                    ["git", "remote", "add", "origin", package.url],
                    [
                        "git",
                        "fetch",
                        "--depth",
                        "1",
                        "--filter=blob:none",
                        "origin",
                        package.revision,
                    ],
                    ["git", "checkout", "--detach", "FETCH_HEAD"],
                ]
                for command in commands:
                    completed = subprocess.run(
                        command,
                        cwd=checkout,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if completed.returncode:
                        raise MaterializationError(
                            f"could not acquire locked source {package.name}",
                            phase="acquisition",
                            command=tuple(command),
                            exit_code=completed.returncode,
                            output=completed.stdout + completed.stderr,
                        )
                tree = subprocess.run(
                    ["git", "rev-parse", "HEAD^{tree}"],
                    cwd=checkout,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                if tree != package.tree_hash:
                    raise MaterializationError(
                        f"acquired Git tree does not match lock for {package.name}",
                        phase="source-validation",
                    )
                self.store.publish_source(
                    checkout,
                    package.source_id,
                    {
                        "schema": "lean-runtime-git-source/1",
                        "source_id": package.source_id,
                        "name": package.name,
                        "url": package.url,
                        "revision": package.revision,
                        "tree_hash": package.tree_hash,
                    },
                )
                self.store.validate_source(
                    package.source_id,
                    url=package.url,
                    revision=package.revision,
                    tree_hash=package.tree_hash,
                )
