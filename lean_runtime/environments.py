"""Published environments and reproducible executions within them."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Generic, Literal, TextIO, TypeVar, cast

from ._git import git_command
from ._paths import remove_tree
from .backends import Backend, BackendResult, InteractiveProcess, InteractiveTextReader
from .diagnostics import error_diagnostic, map_diagnostic_paths, parse_diagnostics
from .errors import EnvironmentError, MaterializationError, PolicyError
from .events import EventEmitter
from .lake import ROOT_MODULE
from .lockfiles import EnvironmentLock
from .locking import FileLock
from .models import (
    ExecutionProvenance,
    ExecutionResult,
    PackageProvenance,
    PhaseTiming,
)
from .policies import ExecutionPolicy
from .references import artifact_accelerators
from .serialization import sha256_id, write_json_atomic
from .store import (
    EnvironmentStore,
    clone_tree,
    environment_identity,
    platform_compatibility,
    platform_record,
)
from .toolchains import ToolchainManager

ENVIRONMENT_SCHEMA = "lean-runtime-published-environment/1"
EXECUTION_SCHEMA = "lean-runtime-execution/1"
CAPTURE_SCHEMA = "lean-runtime-execution-capture/1"
T = TypeVar("T")
_IMPORT = re.compile(r"^\s*import\s+(.+?)\s*$", re.MULTILINE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lean_path(value: str) -> str:
    normalized = PurePosixPath(value.replace("\\", "/"))
    if (
        not value
        or normalized.is_absolute()
        or ".." in normalized.parts
        or normalized.suffix != ".lean"
    ):
        raise EnvironmentError(f"unsafe Lean source path: {value!r}")
    return normalized.as_posix()


def _source_files(files: Mapping[str, str]) -> dict[str, str]:
    if not files:
        raise EnvironmentError("a check requires at least one Lean source file")
    result: dict[str, str] = {}
    for name, source in files.items():
        if not isinstance(name, str) or not isinstance(source, str):
            raise EnvironmentError("Lean source files must map paths to strings")
        normalized = _lean_path(name)
        if normalized in result:
            raise EnvironmentError(f"duplicate normalized Lean source path: {normalized}")
        result[normalized] = source
    return result


def _execution_command(command: Sequence[str]) -> tuple[str, ...]:
    if not command:
        raise EnvironmentError("an execution command must not be empty")
    normalized = tuple(command)
    if any(not isinstance(part, str) or not part or "\0" in part for part in normalized):
        raise EnvironmentError("execution command elements must be non-empty strings without NULs")
    return normalized


def _module_name(path: str) -> str:
    return path.removesuffix(".lean").replace("/", ".")


def _support_order(files: Mapping[str, str], entrypoint: str) -> tuple[str, ...]:
    """Topologically order submitted modules needed before the entrypoint."""
    paths_by_module = {_module_name(path): path for path in files}
    dependencies: dict[str, set[str]] = {}
    for path, source in files.items():
        imported: set[str] = set()
        for match in _IMPORT.finditer(source):
            for module in match.group(1).split():
                if module in paths_by_module:
                    imported.add(paths_by_module[module])
        dependencies[path] = imported

    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(path: str) -> None:
        if path in visited:
            return
        if path in visiting:
            raise EnvironmentError(f"submitted Lean sources contain an import cycle at {path}")
        visiting.add(path)
        for dependency in sorted(dependencies[path]):
            visit(dependency)
        visiting.remove(path)
        visited.add(path)
        ordered.append(path)

    visit(entrypoint)
    return tuple(path for path in ordered if path != entrypoint)


def _package_import_targets(files: Mapping[str, str], lock: EnvironmentLock) -> tuple[str, ...]:
    """Find imported package roots whose artifacts Lake may need on demand."""
    package_names = {package.name.lower() for package in lock.packages}
    package_names.update(
        package.root_module.split(".", 1)[0].lower()
        for package in lock.packages
        if package.root_module
    )
    local_modules = {_module_name(path) for path in files}
    targets: set[str] = set()
    for source in files.values():
        for match in _IMPORT.finditer(source):
            for module in match.group(1).split():
                root = module.split(".", 1)[0]
                if module not in local_modules and root.lower() in package_names:
                    targets.add(root)
    return tuple(sorted(targets))


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
    entrypoint: str
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
            "entrypoint": self.entrypoint,
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
            if not isinstance(name, str) or not isinstance(source, str):
                raise EnvironmentError("execution capture contains an unsafe file entry")
            normalized = _lean_path(name)
            if normalized in normalized_files:
                raise EnvironmentError(
                    f"execution capture contains duplicate source path: {normalized}"
                )
            normalized_files[normalized] = source
        entrypoint_value = value.get("entrypoint")
        if entrypoint_value is None and len(normalized_files) == 1:
            entrypoint_value = next(iter(normalized_files))
        if not isinstance(entrypoint_value, str):
            raise EnvironmentError("execution capture entrypoint must be a Lean source path")
        entrypoint = _lean_path(entrypoint_value)
        if entrypoint not in normalized_files:
            raise EnvironmentError("execution capture entrypoint is not present in files")
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
            entrypoint=entrypoint,
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
        if self._future.done():
            return False
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


class InteractiveSession:
    """One live process inside a disposable environment instance."""

    def __init__(
        self,
        *,
        process: InteractiveProcess,
        execution_id: str,
        finalize: Callable[[BackendResult], ExecutionResult],
        cleanup: Callable[[], None],
    ) -> None:
        self._process = process
        self._execution_id = execution_id
        self._finalize = finalize
        self._cleanup = cleanup
        self._result: ExecutionResult | None = None
        self._closing = threading.Lock()
        self._io = threading.RLock()

    @property
    def stdin(self) -> TextIO:
        return self._process.stdin

    @property
    def stdout(self) -> InteractiveTextReader:
        return self._process.stdout

    @property
    def stderr(self) -> InteractiveTextReader:
        return self._process.stderr

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def running(self) -> bool:
        return self._result is None and self._process.poll() is None

    def poll(self) -> int | None:
        """Return the process exit code, or ``None`` while it is running."""
        return self._process.poll()

    def _reader(self, stream: Literal["stdout", "stderr"]) -> InteractiveTextReader:
        if stream == "stdout":
            return self.stdout
        if stream == "stderr":
            return self.stderr
        raise ValueError("stream must be 'stdout' or 'stderr'")

    def send_line(self, line: str) -> None:
        """Send and flush one line-oriented protocol request."""
        if not isinstance(line, str):
            raise TypeError("interactive request must be a string")
        if "\n" in line or "\r" in line:
            raise ValueError("interactive request must contain exactly one line")
        with self._io:
            if not self.running:
                raise EnvironmentError("interactive process is not running")
            try:
                self.stdin.write(line + "\n")
                self.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise EnvironmentError("interactive process did not accept the request") from exc

    def read_line(self, *, stream: Literal["stdout", "stderr"] = "stdout") -> str:
        """Read one response line from stdout or stderr."""
        with self._io:
            response = self._reader(stream).readline()
            if response == "":
                raise EnvironmentError(
                    f"interactive process ended before producing a {stream} response"
                )
            return response.removesuffix("\n").removesuffix("\r")

    def request_line(
        self,
        line: str,
        *,
        response_stream: Literal["stdout", "stderr"] = "stdout",
    ) -> str:
        """Atomically send one line and read its corresponding response."""
        with self._io:
            self.send_line(line)
            return self.read_line(stream=response_stream)

    def request_json(
        self,
        value: object,
        *,
        response_stream: Literal["stdout", "stderr"] = "stdout",
    ) -> Any:
        """Round-trip one newline-delimited JSON value through the live process."""
        request = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        response = self.request_line(request, response_stream=response_stream)
        try:
            return json.loads(response)
        except json.JSONDecodeError as exc:
            raise EnvironmentError("interactive process returned invalid JSON") from exc

    def close(self) -> ExecutionResult:
        """Send EOF, stop if necessary, persist provenance, and remove the instance."""
        with self._closing:
            if self._result is not None:
                return self._result
            try:
                self._result = self._finalize(self._process.finish())
                return self._result
            finally:
                self._cleanup()

    def __enter__(self) -> InteractiveSession:
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.close()


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
        safe_filename = Path(filename).name
        if not safe_filename.endswith(".lean"):
            safe_filename += ".lean"
        return self.check_files(
            {safe_filename: source},
            entrypoint=safe_filename,
            policy=policy,
            cancel=cancel,
        )

    def check_files(
        self,
        files: Mapping[str, str],
        *,
        entrypoint: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Check a safe relative tree of Lean files through one entrypoint."""
        normalized = _source_files(files)
        selected_entrypoint = _lean_path(entrypoint)
        if selected_entrypoint not in normalized:
            raise EnvironmentError(f"entrypoint is not present in files: {selected_entrypoint}")
        return self._execute_in_instance(
            operation="check",
            files=normalized,
            entrypoint=selected_entrypoint,
            targets=(),
            requested_command=(),
            policy=policy or ExecutionPolicy(),
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

    async def check_async(
        self,
        source: str,
        *,
        filename: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
    ) -> ExecutionResult:
        """Check source without blocking an asyncio event loop."""
        job = self.start_check(source, filename=filename, policy=policy)
        return await self._await_job(job)

    async def check_files_async(
        self,
        files: Mapping[str, str],
        *,
        entrypoint: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
    ) -> ExecutionResult:
        """Check a multi-file request and propagate coroutine cancellation."""
        job: ExecutionJob[ExecutionResult] = ExecutionJob(
            lambda cancel: self.check_files(
                files, entrypoint=entrypoint, policy=policy, cancel=cancel
            )
        )
        return await self._await_job(job)

    @staticmethod
    async def _await_job(job: ExecutionJob[ExecutionResult]) -> ExecutionResult:
        try:
            return await asyncio.to_thread(job.result)
        except asyncio.CancelledError:
            job.cancel()
            while not job.done():
                await asyncio.sleep(0.01)
            job.result()
            raise

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

    async def check_many_async(
        self,
        sources: Sequence[str],
        *,
        concurrency: int = 4,
        policy: ExecutionPolicy | None = None,
    ) -> tuple[ExecutionResult, ...]:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        semaphore = asyncio.Semaphore(concurrency)

        async def check_one(source: str) -> ExecutionResult:
            async with semaphore:
                return await self.check_async(source, policy=policy)

        return tuple(await asyncio.gather(*(check_one(source) for source in sources)))

    def build(
        self,
        targets: Sequence[str] = (),
        *,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        return self._execute_in_instance(
            operation="build",
            files={},
            entrypoint=None,
            targets=tuple(targets),
            requested_command=(),
            policy=policy or ExecutionPolicy(timeout_seconds=900, max_output_bytes=10_000_000),
            cancel=cancel,
        )

    def execute(
        self,
        command: Sequence[str],
        *,
        policy: ExecutionPolicy | None = None,
        cancel: threading.Event | None = None,
    ) -> ExecutionResult:
        """Run a command, such as ``lake exe TARGET``, in a disposable instance."""
        return self._execute_in_instance(
            operation="execute",
            files={},
            entrypoint=None,
            targets=(),
            requested_command=_execution_command(command),
            policy=policy or ExecutionPolicy(),
            cancel=cancel,
        )

    def spawn_interactive(
        self,
        command: Sequence[str],
        *,
        policy: ExecutionPolicy | None = None,
    ) -> InteractiveSession:
        """Start a long-running command with live text pipes in a disposable instance."""
        requested_command = _execution_command(command)
        selected_policy = policy or ExecutionPolicy()
        source_digest = sha256_id("files", {})
        request_digest = sha256_id(
            "request",
            {
                "schema": EXECUTION_SCHEMA,
                "environment_id": self.id,
                "operation": "interactive",
                "source_digest": source_digest,
                "command": list(requested_command),
                "policy": selected_policy.to_dict(),
                "backend": self.manager.backend.name,
            },
        )
        started_at = _now()
        execution_id = sha256_id(
            "execution",
            {
                "request_digest": request_digest,
                "started_at": started_at,
                "nonce": uuid.uuid4().hex,
            },
        )
        job_parent = self.manager.store.jobs / execution_id
        job_parent.mkdir(parents=True, exist_ok=True)
        instance = job_parent / f"instance-{uuid.uuid4().hex}"

        def cleanup() -> None:
            if instance.exists():
                remove_tree(instance)
            with suppress(OSError):
                job_parent.rmdir()

        try:
            with self.manager.store.execution_lease(self.id):
                clone_tree(self.workspace, instance)
            resolved_command = self.manager.toolchains.command(
                self.lock.toolchain, requested_command[0], *requested_command[1:]
            )
            spawn = getattr(self.manager.backend, "spawn_interactive", None)
            if not callable(spawn):
                raise PolicyError(
                    f"backend {self.manager.backend.name!r} does not support interactive execution"
                )
            process = cast(
                InteractiveProcess,
                spawn(
                    resolved_command,
                    cwd=instance,
                    environment=self.manager.toolchains.environment,
                    policy=selected_policy,
                ),
            )
        except BaseException:
            cleanup()
            raise

        def finalize(raw: BackendResult) -> ExecutionResult:
            result = self._result(
                raw,
                command=resolved_command,
                cwd=instance,
                execution_id=execution_id,
                request_digest=request_digest,
                source_digest=source_digest,
                started_at=started_at,
                policy=selected_policy,
            )
            self._record_execution(
                result,
                "interactive",
                (),
                None,
                (),
                requested_command,
            )
            return result

        return InteractiveSession(
            process=process,
            execution_id=execution_id,
            finalize=finalize,
            cleanup=cleanup,
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
        return self.capture_files(
            {safe_filename: source},
            entrypoint=safe_filename,
            policy=policy,
            expected_ok=expected_ok,
        )

    def capture_files(
        self,
        files: Mapping[str, str],
        *,
        entrypoint: str = "Main.lean",
        policy: ExecutionPolicy | None = None,
        expected_ok: bool | None = None,
    ) -> ExecutionCapture:
        normalized = _source_files(files)
        selected_entrypoint = _lean_path(entrypoint)
        if selected_entrypoint not in normalized:
            raise EnvironmentError(f"entrypoint is not present in files: {selected_entrypoint}")
        return ExecutionCapture(
            environment_id=self.id,
            lock=self.lock,
            operation="check",
            files=normalized,
            entrypoint=selected_entrypoint,
            policy=policy or ExecutionPolicy(),
            expected_ok=expected_ok,
        )

    def _execute_in_instance(
        self,
        *,
        operation: str,
        files: dict[str, str],
        entrypoint: str | None,
        targets: tuple[str, ...],
        requested_command: tuple[str, ...],
        policy: ExecutionPolicy,
        cancel: threading.Event | None,
    ) -> ExecutionResult:
        source_digest = sha256_id("files", dict(sorted(files.items())))
        request_digest = sha256_id(
            "request",
            {
                "schema": EXECUTION_SCHEMA,
                "environment_id": self.id,
                "operation": operation,
                "source_digest": source_digest,
                "entrypoint": entrypoint,
                "targets": list(targets),
                "command": list(requested_command),
                "policy": policy.to_dict(),
                "backend": self.manager.backend.name,
            },
        )
        started_at = _now()
        execution_id = sha256_id(
            "execution",
            {
                "request_digest": request_digest,
                "started_at": started_at,
                "nonce": uuid.uuid4().hex,
            },
        )
        job_parent = self.manager.store.jobs / execution_id
        job_parent.mkdir(parents=True, exist_ok=True)
        instance = job_parent / f"instance-{uuid.uuid4().hex}"
        try:
            instance_started = time.monotonic()
            with self.manager.store.execution_lease(self.id):
                clone_tree(self.workspace, instance)
            instance_timing = PhaseTiming(
                "instance_creation", round((time.monotonic() - instance_started) * 1000)
            )
            preliminary: list[BackendResult] = []
            path_map = {str(instance / name): name for name in files}
            staging_started = time.monotonic()
            if operation == "check":
                assert entrypoint is not None
                for name, source in files.items():
                    source_path = instance / name
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    source_path.write_text(source, encoding="utf-8")
                staging_timing = PhaseTiming(
                    "input_staging", round((time.monotonic() - staging_started) * 1000)
                )
                import_targets = _package_import_targets(files, self.lock)
                if import_targets:
                    import_command = self.manager.toolchains.command(
                        self.lock.toolchain, "lake", "build", *import_targets
                    )
                    import_result = self.manager.backend.execute(
                        import_command,
                        cwd=instance,
                        environment=self.manager.toolchains.environment,
                        policy=policy,
                        cancel=cancel,
                    )
                    preliminary.append(import_result)
                    if import_result.exit_code:
                        result = self._result(
                            import_result,
                            command=import_command,
                            cwd=instance,
                            execution_id=execution_id,
                            request_digest=request_digest,
                            source_digest=source_digest,
                            started_at=started_at,
                            policy=policy,
                            timings=(instance_timing, staging_timing),
                            path_map=path_map,
                        )
                        self._record_execution(
                            result,
                            operation,
                            targets,
                            entrypoint,
                            tuple(files),
                            requested_command,
                        )
                        return result
                for support in _support_order(files, entrypoint):
                    support_path = instance / support
                    output_path = (
                        instance / ".lake" / "build" / "lib" / "lean" / support
                    ).with_suffix(".olean")
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    support_command = self.manager.toolchains.command(
                        self.lock.toolchain,
                        "lake",
                        "env",
                        "lean",
                        "-R",
                        str(instance),
                        "-o",
                        str(output_path),
                        str(support_path),
                    )
                    support_result = self.manager.backend.execute(
                        support_command,
                        cwd=instance,
                        environment=self.manager.toolchains.environment,
                        policy=policy,
                        cancel=cancel,
                    )
                    preliminary.append(support_result)
                    if support_result.exit_code:
                        result = self._result(
                            support_result,
                            command=support_command,
                            cwd=instance,
                            execution_id=execution_id,
                            request_digest=request_digest,
                            source_digest=source_digest,
                            started_at=started_at,
                            policy=policy,
                            timings=(instance_timing, staging_timing),
                            path_map=path_map,
                        )
                        self._record_execution(
                            result,
                            operation,
                            targets,
                            entrypoint,
                            tuple(files),
                            requested_command,
                        )
                        return result
                source_path = instance / entrypoint
                command = self.manager.toolchains.command(
                    self.lock.toolchain, "lake", "env", "lean", str(source_path)
                )
            elif operation == "build":
                staging_timing = PhaseTiming("input_staging", 0, performed=False)
                command = self.manager.toolchains.command(
                    self.lock.toolchain, "lake", "build", *targets
                )
            else:
                staging_timing = PhaseTiming("input_staging", 0, performed=False)
                command = self.manager.toolchains.command(
                    self.lock.toolchain, requested_command[0], *requested_command[1:]
                )
            raw = self.manager.backend.execute(
                command,
                cwd=instance,
                environment=self.manager.toolchains.environment,
                policy=policy,
                cancel=cancel,
            )
            if operation == "check" and preliminary:
                raw = BackendResult(
                    exit_code=raw.exit_code,
                    stdout="".join([item.stdout for item in preliminary] + [raw.stdout]),
                    stderr="".join([item.stderr for item in preliminary] + [raw.stderr]),
                    elapsed_seconds=sum(item.elapsed_seconds for item in preliminary)
                    + raw.elapsed_seconds,
                    timed_out=raw.timed_out or any(item.timed_out for item in preliminary),
                    cancelled=raw.cancelled or any(item.cancelled for item in preliminary),
                    output_truncated=raw.output_truncated
                    or any(item.output_truncated for item in preliminary),
                    enforced_policy_fields=raw.enforced_policy_fields,
                )
            result = self._result(
                raw,
                command=command,
                cwd=instance,
                execution_id=execution_id,
                request_digest=request_digest,
                source_digest=source_digest,
                started_at=started_at,
                policy=policy,
                timings=(instance_timing, staging_timing),
                path_map=path_map,
            )
            self._record_execution(
                result,
                operation,
                targets,
                entrypoint,
                tuple(files),
                requested_command,
            )
            return result
        finally:
            if instance.exists():
                remove_tree(instance)
            with suppress(OSError):
                job_parent.rmdir()

    def _result(
        self,
        raw: BackendResult,
        *,
        command: Sequence[str],
        cwd: Path,
        execution_id: str,
        request_digest: str,
        source_digest: str,
        started_at: str,
        policy: ExecutionPolicy,
        timings: tuple[PhaseTiming, ...] = (),
        path_map: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        combined = "\n".join(part for part in (raw.stdout, raw.stderr) if part)
        diagnostics = map_diagnostic_paths(parse_diagnostics(combined), path_map)
        if raw.timed_out:
            diagnostics += (error_diagnostic("Lean execution exceeded its time limit"),)
        if raw.cancelled:
            diagnostics += (error_diagnostic("Lean execution was cancelled"),)
        provenance = ExecutionProvenance(
            environment_id=self.id,
            execution_id=execution_id,
            request_digest=request_digest,
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
            timings=(*timings, PhaseTiming("execution", round(raw.elapsed_seconds * 1000))),
        )

    def _record_execution(
        self,
        result: ExecutionResult,
        operation: str,
        targets: tuple[str, ...],
        entrypoint: str | None,
        files: tuple[str, ...],
        requested_command: tuple[str, ...],
    ) -> None:
        assert result.execution_id is not None
        write_json_atomic(
            self.manager.store.executions / f"{result.execution_id}.json",
            {
                "schema": EXECUTION_SCHEMA,
                "operation": operation,
                "targets": list(targets),
                "entrypoint": entrypoint,
                "files": list(files),
                "command": list(requested_command),
                "result": result.to_dict(),
            },
        )


class EnvironmentManager:
    def __init__(
        self,
        store: EnvironmentStore,
        toolchains: ToolchainManager,
        backend: Backend,
        events: EventEmitter | None = None,
    ) -> None:
        self.store = store
        self.toolchains = toolchains
        self.backend = backend
        self.events = events or EventEmitter()

    def ensure(
        self,
        lock: EnvironmentLock,
        *,
        name: str | None = None,
        build_profile: str = "release",
        build_timeout: float = 1800,
        accelerate: bool = False,
        cancel: threading.Event | None = None,
    ) -> Environment:
        self.store.publish_lock(lock)
        environment_id = environment_identity(lock, build_profile)
        destination = self.store.environment_path(environment_id)
        self.events.emit(
            "environment.ensure_started",
            "Ensuring published environment",
            environment_id=environment_id,
        )
        with FileLock(self.store.lock_dir / f"{environment_id}.lock", timeout=1800, cancel=cancel):
            if not destination.is_dir():
                self._ensure_sources(lock)
                self._materialize(
                    lock,
                    environment_id,
                    destination,
                    build_profile,
                    build_timeout=build_timeout,
                    accelerate=accelerate,
                    cancel=cancel,
                )
            else:
                self.events.emit(
                    "environment.cache_hit",
                    "Reusing published environment",
                    environment_id=environment_id,
                )
        environment = self.open(environment_id)
        if name:
            self.store.set_alias(name, environment_id)
        self.events.emit(
            "environment.ready",
            "Environment is ready",
            environment_id=environment_id,
            name=name,
        )
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
        self.store.touch_environment(environment_id)
        return Environment(self, environment_id, lock, root, record)

    def _materialize(
        self,
        lock: EnvironmentLock,
        environment_id: str,
        destination: Path,
        build_profile: str,
        *,
        build_timeout: float,
        accelerate: bool = False,
        cancel: threading.Event | None,
    ) -> None:
        stage = self.store.environments / f".staging-{os.getpid()}-{uuid.uuid4().hex}"
        workspace = stage / "workspace"
        try:
            self.events.emit(
                "environment.build_started",
                "Building environment",
                environment_id=environment_id,
            )
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
            build_policy = ExecutionPolicy(
                timeout_seconds=build_timeout,
                max_output_bytes=10_000_000,
            )
            hydration: list[dict[str, Any]] = []
            accelerators = artifact_accelerators() if accelerate else {}
            for package in lock.packages:
                requested_command = package.artifact_command or accelerators.get(package.url, ())
                if not requested_command:
                    continue
                accelerated = not package.artifact_command
                self.events.emit(
                    "artifact.hydration_started",
                    f"Hydrating artifacts for {package.name}"
                    + (" (accelerated)" if accelerated else ""),
                    package=package.name,
                    accelerated=accelerated,
                )
                command = list(requested_command)
                if command[0] in {"lake", "lean"}:
                    command = self.toolchains.command(lock.toolchain, command[0], *command[1:])
                result = self.backend.execute(
                    command,
                    cwd=workspace,
                    environment=self.toolchains.environment,
                    policy=build_policy,
                    cancel=cancel,
                )
                hydration.append(
                    {
                        "package": package.name,
                        "command": command,
                        "accelerated": accelerated,
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
                cancel=cancel,
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
                "platform_compatibility": platform_compatibility(),
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
            self.events.emit(
                "environment.published",
                "Published built environment",
                environment_id=environment_id,
            )
        finally:
            if stage.exists():
                # Preserve a build/materialization diagnostic if cleanup of
                # Git's read-only pack files also fails on Windows.
                with suppress(OSError):
                    remove_tree(stage)

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
                self.events.emit(
                    "source.cache_hit",
                    f"Reusing source for {package.name}",
                    package=package.name,
                    source_id=package.source_id,
                )
                continue
            self.events.emit(
                "source.fetch_started",
                f"Fetching {package.name}",
                package=package.name,
                revision=package.revision,
            )
            with tempfile.TemporaryDirectory(
                prefix=f"{package.name}-", dir=acquisition_root
            ) as raw:
                checkout = Path(raw)
                commands = [
                    git_command("init", "-q"),
                    git_command("remote", "add", "origin", package.url),
                    git_command(
                        "fetch",
                        "--depth",
                        "1",
                        "--filter=blob:none",
                        "origin",
                        package.revision,
                    ),
                    git_command("checkout", "--detach", "FETCH_HEAD"),
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
                    git_command("rev-parse", "HEAD^{tree}"),
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
