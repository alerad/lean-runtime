"""Small CI reporting helpers shared by the test suite."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

import pytest

from lean_runtime import EnvironmentLock, LockedPackage
from lean_runtime.discovery import Catalog, CatalogEntry


def make_lock(
    marker: str,
    *,
    toolchain: str = "leanprover/lean4:v4.32.2",
    packages: Iterable[str] = (),
) -> EnvironmentLock:
    locked = tuple(
        LockedPackage(
            name=name,
            url=f"https://example.invalid/{name}.git",
            revision=marker * 40,
            tree_hash=marker * 40,
            source_id=f"source_{marker * 64}",
            root_module=name.title(),
        )
        for name in packages
    )
    return EnvironmentLock(
        toolchain=toolchain,
        spec_digest=f"spec_{marker * 64}",
        root_lakefile=f'name = "fixture-{marker}"\n',
        root_module="",
        manifest={"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []},
        packages=locked,
    )


def make_entry(
    entry_id: str,
    marker: str,
    *,
    modules: Iterable[str] = (),
    packages: Iterable[str] = (),
    created_at: str = "2026-01-01T00:00:00Z",
    toolchain: str = "leanprover/lean4:v4.32.2",
    channel: str = "stable",
) -> CatalogEntry:
    return CatalogEntry(
        id=entry_id,
        channel=channel,
        toolchain=toolchain,
        lock=make_lock(marker, toolchain=toolchain, packages=packages),
        modules=frozenset(modules),
        created_at=created_at,
    )


@pytest.fixture
def sample_catalog() -> Catalog:
    return Catalog(
        generated_at="2026-08-06T00:00:00Z",
        entries=(
            make_entry(
                "mathlib-new",
                "a",
                modules=("Mathlib", "Mathlib.Modern"),
                packages=("mathlib",),
                created_at="2026-08-01T00:00:00Z",
            ),
            make_entry(
                "mathlib-old",
                "b",
                modules=("Mathlib", "Mathlib.Legacy"),
                packages=("mathlib",),
                created_at="2026-07-01T00:00:00Z",
            ),
            make_entry(
                "core",
                "c",
                modules=("Init", "Lean", "Std"),
                created_at="2026-08-01T00:00:00Z",
            ),
        ),
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    """Expose test tracebacks as GitHub annotations without a CI-only plugin."""
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("GITHUB_ACTIONS") == "true" and report.failed and call.excinfo is not None:
        path, line, _ = item.location
        message = str(call.excinfo.getrepr(style="short")).replace("%", "%25")
        message = message.replace("\r", "%0D").replace("\n", "%0A")
        sys.__stdout__.write(
            f"::error file={path},line={line + 1},title=pytest failure::{message}\n"
        )
        sys.__stdout__.flush()
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"### `{item.nodeid}` failed\n\n```text\n")
                handle.write(str(call.excinfo.getrepr(style="short")))
                handle.write("\n```\n")
