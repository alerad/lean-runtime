"""Announcement-gate probe for the documented Python batch API."""

import sys
import time

import lean_runtime as lean

env = lean.setup(lock=sys.argv[1])
proofs = [f"import Mathlib\nexample : {i} + 0 = {i} := by norm_num" for i in range(4)] + [
    "import Mathlib\nexample : (1 : Nat) = 2 := by norm_num"
]
started = time.time()
results = env.check_many(
    proofs, concurrency=2, policy=lean.ExecutionPolicy(timeout_seconds=600)
)
ok = sum(r.ok for r in results)
assert ok == 4, f"expected 4/5 accepted, got {ok}/5"
bad = results[-1]
assert not bad.ok and bad.first_error is not None, "failing proof must expose first_error"
assert "instance-" not in (bad.first_error.file or ""), bad.first_error.file
print(f"python batch API ok: {ok}/5 accepted in {time.time() - started:.1f}s")
