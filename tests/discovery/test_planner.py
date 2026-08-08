from conftest import make_entry

from lean_runtime.discovery import (
    AvailabilityObservation,
    Discovery,
    DiscoveryPolicy,
)


def test_exact_module_match_beats_newest_release(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    plan = Discovery(catalog=sample_catalog).plan("import Mathlib.Legacy\n")
    assert [candidate.entry.id for candidate in plan.candidates] == ["mathlib-old"]
    assert {item.entry_id for item in plan.excluded} == {"core", "mathlib-new"}


def test_newest_breaks_ties_after_evidence(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    plan = Discovery(catalog=sample_catalog).plan("import Mathlib\n")
    assert [candidate.entry.id for candidate in plan.candidates] == [
        "mathlib-new",
        "mathlib-old",
    ]


def test_local_availability_can_break_tie(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    old = next(entry for entry in sample_catalog.entries if entry.id == "mathlib-old")
    plan = Discovery(
        catalog=sample_catalog,
        availability={old.lock.lock_id: AvailabilityObservation(local=True)},
    ).plan("import Mathlib\n")
    assert plan.candidates[0].entry.id == "mathlib-old"


def test_candidate_limit_is_strict(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    plan = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_candidates=1),
    ).plan("import Mathlib\n")
    assert len(plan.candidates) == 1
    assert plan.total_plausible_candidates == 2
    assert plan.truncated


def test_explicit_lock_bypasses_catalog(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    source = """-- /// lean-runtime
-- lock = "environment.lock.json"
-- ///
example : True := trivial
"""
    plan = Discovery(catalog=sample_catalog).plan(source)
    assert plan.explicit_lock == "environment.lock.json"
    assert plan.candidates == ()


def test_core_only_prefers_core(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    plan = Discovery(catalog=sample_catalog).plan("example : True := trivial\n")
    assert [candidate.entry.id for candidate in plan.candidates] == ["core"]


def test_toolchain_hint_is_hard_constraint() -> None:
    old = make_entry("old", "d", toolchain="leanprover/lean4:v4.31.0")
    new = make_entry("new", "e", toolchain="leanprover/lean4:v4.32.2")
    from lean_runtime.discovery import Catalog

    catalog = Catalog(generated_at="2026-08-06T00:00:00Z", entries=(old, new))
    source = """-- /// lean-runtime
-- toolchain = "leanprover/lean4:v4.31.0"
-- ///
example : True := trivial
"""
    plan = Discovery(catalog=catalog).plan(source)
    assert [candidate.entry.id for candidate in plan.candidates] == ["old"]


def test_plan_never_claims_compilation(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    payload = Discovery(catalog=sample_catalog).plan("import Mathlib\n").to_dict()
    assert payload["confidence"] == "heuristic_only"
