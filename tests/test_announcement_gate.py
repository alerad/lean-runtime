from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

plan_data = cast(
    Callable[[object], dict[str, object]],
    run_path(str(Path(__file__).parents[1] / "scripts" / "announcement_gate.py"))["plan_data"],
)


def test_announcement_gate_reads_budget_fields_from_plan_envelope() -> None:
    assert plan_data(
        {
            "schema": "lean-runtime.plan/v1",
            "ok": True,
            "errors": [],
            "warnings": [],
            "data": {
                "download_bytes": 200,
                "environment_download_bytes": 100,
                "download_bytes_complete": True,
            },
        }
    ) == {
        "download_bytes": 200,
        "environment_download_bytes": 100,
        "download_bytes_complete": True,
    }


def test_announcement_gate_rejects_failed_or_unversioned_plan_envelopes() -> None:
    assert plan_data({"ok": True, "data": {"download_bytes_complete": True}}) == {}
    assert (
        plan_data(
            {
                "schema": "lean-runtime.plan/v1",
                "ok": False,
                "data": {"download_bytes_complete": True},
            }
        )
        == {}
    )
