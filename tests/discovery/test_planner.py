from conftest import make_entry

from lean_runtime.discovery import (
    AvailabilityObservation,
    Discovery,
    DiscoveryPolicy,
    default_catalog,
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


def test_smallest_compatible_environment_breaks_tie() -> None:
    from lean_runtime.discovery import Catalog

    mathlib = make_entry(
        "mathlib",
        "d",
        modules=("Mathlib",),
        packages=("mathlib",),
        created_at="2026-08-10T00:00:00Z",
    )
    leancert = make_entry(
        "leancert",
        "e",
        modules=("Mathlib", "LeanCert"),
        packages=("mathlib", "leancert"),
        created_at="2026-08-11T00:00:00Z",
    )
    catalog = Catalog(generated_at="2026-08-12T00:00:00Z", entries=(leancert, mathlib))

    mathlib_plan = Discovery(catalog=catalog).plan("import Mathlib\n")
    assert [candidate.entry.id for candidate in mathlib_plan.candidates] == [
        "mathlib",
        "leancert",
    ]

    leancert_plan = Discovery(catalog=catalog).plan("import LeanCert\n")
    assert [candidate.entry.id for candidate in leancert_plan.candidates] == ["leancert"]


def test_local_extension_can_still_beat_smaller_remote_environment() -> None:
    from lean_runtime.discovery import Catalog

    mathlib = make_entry("mathlib", "d", modules=("Mathlib",), packages=("mathlib",))
    leancert = make_entry(
        "leancert",
        "e",
        modules=("Mathlib", "LeanCert"),
        packages=("mathlib", "leancert"),
    )
    catalog = Catalog(generated_at="2026-08-12T00:00:00Z", entries=(mathlib, leancert))
    plan = Discovery(
        catalog=catalog,
        availability={leancert.lock.lock_id: AvailabilityObservation(local=True)},
    ).plan("import Mathlib\n")

    assert plan.candidates[0].entry.id == "leancert"


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


def test_bundled_catalog_keeps_mathlib_and_leancert_discovery_distinct() -> None:
    discovery = Discovery(catalog=default_catalog())

    mathlib = discovery.plan("import Mathlib\n")
    leancert = discovery.plan("import LeanCert\n")

    assert mathlib.candidates[0].entry.id == "mathlib-v4.33.0"
    assert leancert.candidates[0].entry.id == "leancert-v4.33.0"


def test_compatibility_profiles_only_check_advertised_catalog_roots() -> None:
    import json
    from pathlib import Path

    catalog = default_catalog()
    entries = {entry.id: entry for entry in catalog.entries}
    root = Path(__file__).parents[2] / "compatibility"
    for version in ("4.32.2", "4.33.0"):
        profile = json.loads((root / f"mathlib-{version}.json").read_text())
        entry = entries[f"mathlib-v{version}"]
        assert set(profile["imports"]) <= set(entry.modules)
