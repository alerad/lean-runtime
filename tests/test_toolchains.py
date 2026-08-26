from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import ProjectError, ToolchainError, normalize_toolchain, project_toolchain
from lean_runtime.events import EventEmitter
from lean_runtime.toolchains import (
    ELAN_RELEASE_URL,
    ToolchainManager,
    elan_release_archive,
    immutable_toolchain_spelling,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.32.0", "leanprover/lean4:v4.32.0"),
        ("v4.31.0", "leanprover/lean4:v4.31.0"),
        ("leanprover/lean4:v4.30.0", "leanprover/lean4:v4.30.0"),
        ("nightly-2026-01-01", "nightly-2026-01-01"),
    ],
)
def test_normalize_toolchain(raw: str, expected: str) -> None:
    assert normalize_toolchain(raw) == expected


def test_empty_toolchain_is_rejected() -> None:
    with pytest.raises(ToolchainError):
        normalize_toolchain("  ")


def test_project_toolchain(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    assert project_toolchain(tmp_path) == "leanprover/lean4:v4.32.0"


def test_project_without_toolchain_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        project_toolchain(tmp_path)


def test_explicit_elan_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / ("elan.exe" if os.name == "nt" else "elan")
    executable.write_text("")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN", str(executable))
    assert ToolchainManager(tmp_path / "runtime").elan_path() == executable.absolute()


def test_elan_on_path_is_reused_without_private_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / ("elan.exe" if os.name == "nt" else "elan")
    executable.write_text("")
    monkeypatch.delenv("LEAN_RUNTIME_ELAN", raising=False)
    monkeypatch.setattr("lean_runtime.toolchains.shutil.which", lambda _name: str(executable))

    assert ToolchainManager(tmp_path / "runtime").elan_path() == executable.absolute()


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlink creation")
def test_elan_override_preserves_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elan-init"
    target.write_text("")
    link = tmp_path / "elan"
    link.symlink_to(target)
    monkeypatch.setenv("LEAN_RUNTIME_ELAN", str(link))
    assert ToolchainManager(tmp_path / "runtime").elan_path() == link.absolute()


@pytest.mark.skipif(os.name == "nt", reason="Windows bootstrap uses an existing Elan executable")
def test_elan_bootstrap_rejects_installer_with_wrong_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(b"bad"))
    with pytest.raises(ToolchainError, match="integrity check"):
        ToolchainManager(tmp_path / "runtime").bootstrap_elan()


def _fake_elan_release(script: str) -> bytes:
    """Build a release-shaped tar.gz whose elan-init is a shell script."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        payload = script.encode()
        info = tarfile.TarInfo("elan-init")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="Windows bootstrap uses an existing Elan executable")
def test_elan_bootstrap_installs_the_pinned_release_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _fake_elan_release(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$ELAN_HOME.args"\n'
        'mkdir -p "$ELAN_HOME/bin" && printf "" > "$ELAN_HOME/bin/elan" '
        '&& chmod +x "$ELAN_HOME/bin/elan"\n'
    )
    digest = hashlib.sha256(archive).hexdigest()
    requested: list[str] = []

    def urlopen(url: str, **_kwargs: object) -> io.BytesIO:
        requested.append(url)
        return io.BytesIO(archive)

    monkeypatch.setattr(
        "lean_runtime.toolchains.elan_release_archive", lambda: ("elan-test.tar.gz", digest)
    )
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.delenv("LEAN_RUNTIME_ELAN", raising=False)
    monkeypatch.delenv("LEAN_RUNTIME_ELAN_HOME", raising=False)
    monkeypatch.setattr("lean_runtime.toolchains.shutil.which", lambda _name: None)

    manager = ToolchainManager(tmp_path / "runtime")
    assert manager.bootstrap_elan() == manager.elan_home / "bin" / "elan"
    assert requested == [f"{ELAN_RELEASE_URL}/elan-test.tar.gz"]
    recorded = Path(f"{manager.elan_home}.args").read_text().split()
    assert recorded == ["-y", "--no-modify-path", "--default-toolchain", "none"]


@pytest.mark.parametrize(
    ("system", "machine", "archive"),
    [
        ("Linux", "x86_64", "elan-x86_64-unknown-linux-gnu.tar.gz"),
        ("Linux", "aarch64", "elan-aarch64-unknown-linux-gnu.tar.gz"),
        ("Darwin", "arm64", "elan-aarch64-apple-darwin.tar.gz"),
        ("Darwin", "x86_64", "elan-x86_64-apple-darwin.tar.gz"),
    ],
)
def test_elan_release_archive_is_selected_per_platform(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, archive: str
) -> None:
    monkeypatch.setattr("lean_runtime.toolchains.platform.system", lambda: system)
    monkeypatch.setattr("lean_runtime.toolchains.platform.machine", lambda: machine)
    monkeypatch.setattr("lean_runtime.toolchains.platform.libc_ver", lambda: ("glibc", "2.39"))
    name, digest = elan_release_archive()
    assert name == archive
    assert len(digest) == 64


def test_elan_release_archive_refuses_platforms_without_a_pinned_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_runtime.toolchains.platform.system", lambda: "Linux")
    monkeypatch.setattr("lean_runtime.toolchains.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("lean_runtime.toolchains.platform.libc_ver", lambda: ("musl", "1.2"))
    with pytest.raises(ToolchainError, match="LEAN_RUNTIME_ELAN"):
        elan_release_archive()
    monkeypatch.setattr("lean_runtime.toolchains.platform.system", lambda: "FreeBSD")
    with pytest.raises(ToolchainError, match="freebsd/x86_64"):
        elan_release_archive()


def test_is_installed_lists_toolchains_without_running_lean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "elan"
    executable.write_text("")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN", str(executable))
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "leanprover/lean4:v4.32.0 (default)\nleanprover/lean4:v4.31.0\n",
        )

    monkeypatch.setattr(subprocess, "run", run)
    manager = ToolchainManager(tmp_path / "runtime")
    assert manager.is_installed("4.32.0")
    assert not manager.is_installed("4.30.0")
    assert all(command[1:] == ["toolchain", "list"] for command in calls)


def test_existing_user_elan_toolchain_is_reused_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user"
    elan_home = user_home / ".elan"
    elan = elan_home / "bin" / ("elan.exe" if os.name == "nt" else "elan")
    elan.parent.mkdir(parents=True)
    elan.write_text("")
    toolchain = "leanprover/lean4:v4.33.0"
    directory = elan_home / "toolchains" / "leanprover--lean4---v4.33.0" / "bin"
    directory.mkdir(parents=True)
    lean_name = "lean.exe" if os.name == "nt" else "lean"
    lake_name = "lake.exe" if os.name == "nt" else "lake"
    (directory / lean_name).write_text("")
    (directory / lake_name).write_text("")
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("PATH", str(elan.parent))
    monkeypatch.delenv("LEAN_RUNTIME_ELAN", raising=False)
    monkeypatch.delenv("LEAN_RUNTIME_ELAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: user_home)

    manager = ToolchainManager(tmp_path / "runtime")
    assert manager.ensure_full(toolchain) == toolchain
    assert manager.command(toolchain, "lean", "--version") == [
        str(directory / lean_name),
        "--version",
    ]
    execution_path = manager.environment_for(toolchain)["PATH"].split(os.pathsep)
    assert execution_path[0] == str(directory)
    assert execution_path[1] == str(manager.elan_home / "bin")
    monkeypatch.setattr(manager, "has_slim", lambda _name: True)
    with pytest.raises(ToolchainError, match="user-managed"):
        manager.prune_original(toolchain)


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX executable test fixtures")
def test_selected_full_toolchain_controls_nested_lake_and_lean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    toolchain = "leanprover/lean4:v4.33.0"
    selected = manager._elan_toolchain_dir(toolchain) / "bin"
    selected.mkdir(parents=True)
    lean = selected / "lean"
    lean.write_text("#!/bin/sh\nprintf 'selected lean\\n'\n")
    lean.chmod(0o755)
    lake = selected / "lake"
    lake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$(command -v lake)" "$(command -v lean)"\nlean\n')
    lake.chmod(0o755)

    wrong = tmp_path / "wrong" / "bin"
    wrong.mkdir(parents=True)
    for name in ("lake", "lean"):
        executable = wrong / name
        executable.write_text(f"#!/bin/sh\nprintf 'wrong {name}\\n'\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(wrong))

    process = subprocess.run(
        manager.command(toolchain, "lake"),
        env=manager.environment_for(toolchain),
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    assert process.stdout.splitlines() == [str(lake), str(lean), "selected lean"]


def test_package_manager_elan_uses_the_default_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user"
    manager_bin = tmp_path / "package-manager" / "bin"
    manager_bin.mkdir(parents=True)
    elan = manager_bin / ("elan.exe" if os.name == "nt" else "elan")
    elan.write_text("#!/bin/sh\n")
    elan.chmod(0o755)
    toolchain = "leanprover/lean4:v4.32.2"
    lean = (
        user_home
        / ".elan"
        / "toolchains"
        / "leanprover--lean4---v4.32.2"
        / "bin"
        / ("lean.exe" if os.name == "nt" else "lean")
    )
    lean.parent.mkdir(parents=True)
    lean.write_text("")
    (lean.parent / ("lake.exe" if os.name == "nt" else "lake")).write_text("")
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("PATH", str(manager_bin))
    monkeypatch.delenv("LEAN_RUNTIME_ELAN", raising=False)
    monkeypatch.delenv("LEAN_RUNTIME_ELAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: user_home)

    manager = ToolchainManager(tmp_path / "runtime")
    assert manager.command(toolchain, "lean") == [str(lean)]


def test_executable_digest_identifies_the_exact_toolchain_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    toolchain = "leanprover/lean4:v4.33.0"
    binary = manager._elan_toolchain_dir(toolchain) / "bin" / "lake"
    binary.parent.mkdir(parents=True)
    (binary.parent / "lean").write_bytes(b"exact lean binary")
    binary.write_bytes(b"exact lake binary")
    monkeypatch.setattr(manager, "ensure", lambda _toolchain: toolchain)

    assert manager.executable_digest(toolchain, "lake") == (
        "sha256:" + hashlib.sha256(b"exact lake binary").hexdigest()
    )


@pytest.mark.skipif(
    os.name == "nt", reason="Windows preserves creation time when replacing file contents in place"
)
def test_executable_digest_rehashes_same_size_replacement_with_restored_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    toolchain = "leanprover/lean4:v4.33.0"
    binary = manager._binary(manager._elan_toolchain_dir(toolchain), "lean")
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"first")
    monkeypatch.setattr(manager, "ensure", lambda _toolchain: toolchain)
    first_stat = binary.stat()
    assert manager.executable_digest(toolchain, "lean") == (
        "sha256:" + hashlib.sha256(b"first").hexdigest()
    )

    binary.write_bytes(b"other")
    os.utime(binary, ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns))

    assert manager.executable_digest(toolchain, "lean") == (
        "sha256:" + hashlib.sha256(b"other").hexdigest()
    )


@pytest.mark.parametrize(
    ("toolchain", "expected"),
    [
        ("4.33.0", True),
        ("leanprover/lean4:v4.34.0-rc1", True),
        ("nightly-2026-08-24", True),
        ("leanprover/lean4:master", False),
        ("nightly", False),
        ("stable", False),
        ("custom-linked-toolchain", False),
    ],
)
def test_publication_toolchain_spelling_must_be_immutable(toolchain: str, expected: bool) -> None:
    assert immutable_toolchain_spelling(toolchain) is expected


def test_ensure_full_does_not_accept_a_slim_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    toolchain = "leanprover/lean4:v4.33.0"
    slim = manager.slim_path(toolchain)
    (slim / "bin").mkdir(parents=True)
    manager._binary(slim, "lean").write_text("")
    calls: list[list[str]] = []

    def execute(_backend, command, **_kwargs):
        calls.append(list(command))
        full = manager._elan_toolchain_dir(toolchain) / "bin"
        full.mkdir(parents=True)
        manager._binary(full.parent, "lean").write_text("")
        manager._binary(full.parent, "lake").write_text("")
        from lean_runtime.backends import BackendResult

        return BackendResult(0, "", "", 0.01, False, False, False, ())

    elan = tmp_path / "elan"
    elan.write_text("")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN", str(elan))
    monkeypatch.setattr("lean_runtime.toolchains.LocalBackend.execute", execute)

    assert manager.ensure_full(toolchain) == toolchain
    assert calls and calls[0][-3:] == ["toolchain", "install", toolchain]


def _install_process(exit_code: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(exit_code=exit_code, cancelled=False, stdout="", stderr=stderr)


def test_transient_download_failures_are_retried_then_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN_HOME", str(manager.elan_home))
    manager.install_retry_delays = (0.0, 0.0)
    monkeypatch.setattr(manager, "elan_path", lambda **_kwargs: tmp_path / "elan")
    monkeypatch.setattr(manager, "is_installed", lambda _name: False)
    outcomes = iter(
        [
            _install_process(
                1,
                "error: could not download file from 'https://releases.lean-lang.org/x'\n"
                "info: caused by: [35] SSL connect error (TLS connect error)\n",
            ),
            _install_process(1, "info: caused by: error during download\n"),
            _install_process(0),
        ]
    )
    commands: list[list[str]] = []

    def execute(_self, command, **_kwargs):
        commands.append(list(command))
        return next(outcomes)

    monkeypatch.setattr("lean_runtime.toolchains.LocalBackend.execute", execute)
    events: list[str] = []
    manager.events = EventEmitter(lambda event: events.append(event.kind))

    assert manager.ensure("leanprover/lean4:v4.32.0") == "leanprover/lean4:v4.32.0"
    assert len(commands) == 3
    assert all(
        command[1:] == ["toolchain", "install", "leanprover/lean4:v4.32.0"] for command in commands
    )
    assert events.count("toolchain.install_retry") == 2


def test_non_download_install_failures_are_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN_HOME", str(manager.elan_home))
    manager.install_retry_delays = (0.0, 0.0)
    monkeypatch.setattr(manager, "elan_path", lambda **_kwargs: tmp_path / "elan")
    monkeypatch.setattr(manager, "is_installed", lambda _name: False)
    calls = 0

    def execute(_self, command, **_kwargs):
        nonlocal calls
        calls += 1
        return _install_process(1, "error: unknown toolchain 'leanprover/lean4:v9.99.0'\n")

    monkeypatch.setattr("lean_runtime.toolchains.LocalBackend.execute", execute)
    with pytest.raises(ToolchainError, match="unknown toolchain"):
        manager.ensure("leanprover/lean4:v9.99.0")
    assert calls == 1


def test_exhausted_download_retries_report_the_attempt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN_HOME", str(manager.elan_home))
    manager.install_retry_delays = (0.0,)
    monkeypatch.setattr(manager, "elan_path", lambda **_kwargs: tmp_path / "elan")
    monkeypatch.setattr(
        "lean_runtime.toolchains.LocalBackend.execute",
        lambda _self, _command, **_kwargs: _install_process(1, "error during download\n"),
    )
    with pytest.raises(ToolchainError, match="after 2 attempts"):
        manager.ensure_full("leanprover/lean4:v4.32.0")
