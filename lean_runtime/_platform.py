"""Internal host platform identity and build-compatibility records."""

from __future__ import annotations

import platform

PLATFORM_COMPATIBILITY_SCHEMA = "lean-runtime-platform/1"


def platform_record() -> dict[str, str]:
    return {
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "python_platform": platform.platform(),
    }


def platform_compatibility() -> dict[str, str]:
    """Return only fields that determine compatibility of built artifacts."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    abi = "native"
    if system == "linux":
        libc, _version = platform.libc_ver()
        abi = {"glibc": "gnu", "musl": "musl"}.get(libc.lower(), libc.lower() or "unknown")
    elif system == "windows":
        abi = "msvc"
    return {
        "schema": PLATFORM_COMPATIBILITY_SCHEMA,
        "system": system,
        "machine": machine,
        "abi": abi,
    }
