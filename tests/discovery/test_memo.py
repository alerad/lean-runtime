from lean_runtime.discovery import DiscoveryPolicy, analyze_source
from lean_runtime.discovery.history import DiscoveryHistory
from lean_runtime.discovery.memo import DecisionMemo


def test_decision_memo_distinguishes_exact_source_from_header_hint(tmp_path) -> None:
    memo = DecisionMemo(tmp_path)
    policy = DiscoveryPolicy()
    source = "import Mathlib\nexample : True := trivial\n"
    evidence = analyze_source(source)
    memo.remember(source, evidence, "sha256:catalog", policy, "lock_selected")

    exact = memo.lookup(source, evidence, "sha256:catalog", policy)
    assert exact is not None and exact.lock_id == "lock_selected" and exact.exact_source

    changed = "import Mathlib\nexample : 1 = 1 := rfl\n"
    header = memo.lookup(changed, analyze_source(changed), "sha256:catalog", policy)
    assert header is not None and header.lock_id == "lock_selected" and not header.exact_source

    # Catalog and timeout policy changes do not invalidate compiler evidence.
    assert memo.lookup(source, evidence, "sha256:other", policy) == exact


def test_history_remembers_only_exact_compiler_rejections(tmp_path) -> None:
    history = DiscoveryHistory(tmp_path)
    source = "import Mathlib\nexample : False := by trivial\n"
    history.remember_rejection(source, "lock_bad")
    history.remember_rejection(source, "lock_bad")
    assert history.rejected_locks(source) == frozenset({"lock_bad"})

    evidence = analyze_source(source)
    history.remember_success(source, evidence, "lock_bad")
    assert history.rejected_locks(source) == frozenset()
    assert history.lookup(source, evidence) is not None
