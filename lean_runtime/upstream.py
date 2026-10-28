"""Best-effort detection of Mathlib releases newer than the catalog.

The catalog (bundled plus published additions) is the only source of versions
Lean Runtime installs by default; this lookup never changes that. It exists so
`new` and `update` can say when "latest" is behind upstream instead of silently
presenting a stale release as current. Every failure (offline, slow network,
missing Git) reads as unknown, and results are remembered in the Runtime home so
the network is not asked every time.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ._process import run_git

MATHLIB_URL = "https://github.com/leanprover-community/mathlib4.git"
DISABLE_ENVIRONMENT = "LEAN_RUNTIME_NO_UPSTREAM_CHECK"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60
RETRY_AFTER_FAILURE_SECONDS = 30 * 60
_STABLE_REF = re.compile(r"refs/tags/v(\d+\.\d+\.\d+)")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split(".") if part.isdigit())


def newest_stable_tag(ls_remote_output: str) -> str | None:
    """Return the newest `X.Y.Z` release named in `git ls-remote --tags` output."""
    versions = [
        match.group(1)
        for line in ls_remote_output.splitlines()
        if (match := _STABLE_REF.fullmatch(line.partition("\t")[2].strip())) is not None
    ]
    return max(versions, key=version_key, default=None)


def _newest_upstream(home: Path | None, timeout: float) -> str | None:
    """Newest stable upstream tag, remembered in the Runtime home between commands."""
    record = home / "catalog" / "upstream.json" if home is not None else None
    now = time.time()
    if record is not None:
        try:
            value = json.loads(record.read_text(encoding="utf-8"))
            if now < float(value["next_check_at"]):
                newest = value.get("newest")
                return newest if isinstance(newest, str) else None
        except (OSError, ValueError, KeyError, TypeError):
            pass
    outcome = run_git("ls-remote", "--tags", "--refs", MATHLIB_URL, "refs/tags/v*", timeout=timeout)
    newest = newest_stable_tag(outcome.stdout) if outcome.ok else None
    if record is not None:
        delay = CHECK_INTERVAL_SECONDS if newest is not None else RETRY_AFTER_FAILURE_SECONDS
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            temporary = record.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"next_check_at": now + delay, "newest": newest}), encoding="utf-8"
            )
            os.replace(temporary, record)
        except OSError:
            pass
    return newest


def newer_upstream_mathlib(
    cataloged: str, *, home: str | os.PathLike[str] | None = None, timeout: float = 5.0
) -> str | None:
    """Return the newest upstream stable Mathlib when it is newer than `cataloged`."""
    if os.environ.get(DISABLE_ENVIRONMENT):
        return None
    newest = _newest_upstream(Path(home) if home is not None else None, timeout)
    if newest is None or version_key(newest) <= version_key(cataloged):
        return None
    return newest


def upstream_notice(cataloged: str, newest: str, *, command: str) -> str:
    message = (
        f"Note: Mathlib {newest} is released upstream but not cataloged yet "
        f"(newest cataloged: {cataloged}); it is offered automatically once published."
    )
    if command == "new":
        message += (
            f" To use it now, pass --mathlib {newest} --uncataloged "
            "(resolved from its tag, without published Lean Runtime artifacts)."
        )
    return message
