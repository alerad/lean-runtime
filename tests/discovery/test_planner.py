import json

from conftest import make_entry
from jsonschema import Draft202012Validator

from lean_runtime.discovery import Discovery, DiscoveryPolicy, default_catalog, schema_path


def test_exact_module_match_beats_newest_release(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    plan = Discovery(catalog=sample_catalog).plan("import Mathlib.Legacy\n")
    assert [candidate.entry.id for candidate in plan.candidates] == [
        "mathlib-old",
        "core",
        "mathlib-new",
    ]
    assert plan.excluded == ()


def test_newest_breaks_ties_after_evidence(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    plan = Discovery(catalog=sample_catalog).plan("import Mathlib\n")
    assert [candidate.entry.id for candidate in plan.candidates] == [
        "mathlib-new",
        "mathlib-old",
        "core",
    ]


def test_local_availability_can_break_tie(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    old = next(entry for entry in sample_catalog.entries if entry.id == "mathlib-old")
    discovery = Discovery(catalog=sample_catalog)
    static = discovery.plan("import Mathlib\n")
    plan = discovery.planner.order(static, local_lock_ids=frozenset({old.lock.lock_id}))
    assert plan.candidates[0].entry.id == "mathlib-old"


def test_decision_hint_is_tried_first_without_bypassing_candidates(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    old = next(entry for entry in sample_catalog.entries if entry.id == "mathlib-old")
    evidence = Discovery(catalog=sample_catalog).analyze("import Mathlib\n")
    discovery = Discovery(catalog=sample_catalog)
    plan = discovery.planner.order(
        discovery.planner.plan(evidence, sample_catalog, DiscoveryPolicy()),
        preferred_lock_id=old.lock.lock_id,
        preferred_exact=True,
    )
    assert [candidate.entry.id for candidate in plan.candidates] == [
        "mathlib-old",
        "mathlib-new",
        "core",
    ]


def test_negative_history_never_hides_every_compiler_diagnostic(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    discovery = Discovery(catalog=sample_catalog)
    static = discovery.plan("import Mathlib\n")
    all_locks = frozenset(candidate.entry.lock.lock_id for candidate in static.candidates)
    ordered = discovery.planner.order(static, rejected_lock_ids=all_locks)
    assert ordered.candidates == static.candidates


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
    assert [candidate.entry.id for candidate in leancert_plan.candidates] == [
        "leancert",
        "mathlib",
    ]


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
    discovery = Discovery(catalog=catalog)
    plan = discovery.planner.order(
        discovery.plan("import Mathlib\n"),
        local_lock_ids=frozenset({leancert.lock.lock_id}),
    )

    assert plan.candidates[0].entry.id == "leancert"


def test_candidate_limit_is_strict(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    plan = Discovery(
        catalog=sample_catalog,
        policy=DiscoveryPolicy(max_candidates=1),
    ).plan("import Mathlib\n")
    assert len(plan.candidates) == 3
    assert len(plan.planned_candidates) == 1
    assert plan.total_plausible_candidates == 3
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
    Draft202012Validator(json.loads(schema_path("plan-v1.schema.json").read_text())).validate(
        payload
    )


def test_bundled_catalog_keeps_mathlib_and_leancert_discovery_distinct() -> None:
    discovery = Discovery(catalog=default_catalog())

    mathlib = discovery.plan("import Mathlib\n")
    leancert = discovery.plan("import LeanCert\n")

    assert mathlib.candidates[0].entry.id == "mathlib-v4.33.0"
    assert leancert.candidates[0].entry.id == "leancert-v4.33.0"


def test_mathlib_source_plans_successive_mathlib_releases() -> None:
    plan = Discovery(catalog=default_catalog()).plan("import Mathlib\n")
    assert [candidate.entry.id for candidate in plan.planned_candidates] == [
        "mathlib-v4.33.0",
        "mathlib-v4.32.2",
        "mathlib-v4.31.0",
    ]


def test_planned_candidates_never_repeat_one_mathlib_tree() -> None:
    """A bounded search must spend each acquisition on a distinct hypothesis.

    Every ``leancert-*`` entry bundles the identical Mathlib revision and tree
    as its ``mathlib-*`` sibling, so probing both cannot change a Mathlib
    verdict. Planning them adjacently exhausted ``max_remote_acquisitions``
    against one hypothesis and left older Mathlib releases untried.
    """
    plan = Discovery(catalog=default_catalog()).plan("import Mathlib\n")

    def mathlib_tree(candidate):  # type: ignore[no-untyped-def]
        package = next(
            (item for item in candidate.entry.lock.packages if item.name == "mathlib"), None
        )
        return None if package is None else (package.revision, package.tree_hash)

    planned = plan.planned_candidates
    trees = [mathlib_tree(candidate) for candidate in planned]
    assert len(set(trees)) == len(trees), [candidate.entry.id for candidate in planned]


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
