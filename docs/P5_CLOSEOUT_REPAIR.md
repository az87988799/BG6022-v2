# f5991d0 review: limited closeout repair

Status: limited code repair verified; **PENDING_OWNER_ACCEPTANCE**.
This does not authorize merge, P5 PASS or starting P6.

Implementation: `7c68e6a9b26d49e825b45db1784360ec366119a0`.
[CI 34100584094](https://github.com/az87988799/BG6022-v2/actions/runs/34100584094)
is fully green: Windows 527 passed; Ubuntu 3.11/3.14 525 passed, 2 Windows-only
skips; Ubuntu branch coverage 80.31% with the unchanged 80% gate; quality PASS.
Targeted local suite: 67 passed, including Windows process tests; final backend
guard replay: 10 passed. Ruff, format, compileall, lock check and wheel/sdist
build passed. The final evidence-only PR CI [34119947306](https://github.com/az87988799/BG6022-v2/actions/runs/34119947306)
is also green: Ubuntu 3.14 526 passed, 2 skipped, 25 warnings; Windows 3.14,
Ubuntu 3.11 and the 80% branch-coverage gate passed. [Separate offline replay
and new preview](evidence/p5-closeout/README.md).

## Parser v4: translation-only Hessian binding

After validating atom count, element ordering and default atomic masses (existing
0.02 amu tolerance), convert Hessian `$atoms` Bohr coordinates with
`0.529177210903 Å/Bohr`. For each coordinate set, calculate the center of mass
using the **same validated Hessian mass for each corresponding atom**:
`COM[k] = sum(m[i] * xyz[i][k]) / sum(m[i])`. Subtract that set's COM, then compare
every corresponding Cartesian component with the unchanged `2e-6 Å` tolerance.
Only a common translation is allowed. No rotation, reflection, atom permutation,
unit guessing or enlarged geometry tolerance is introduced.

Finite-number, 3N matrix completeness/symmetry, frequency and atom/mass checks
remain intact. Original XYZ, stdout and Hessian bytes/hashes are never normalized
or rewritten. `orca-parser-v4` records the changed contract; compiler, ticket,
shared worker and database migrations are unchanged.

`tests/p5/test_p5_closeout_review.py` replays the committed real Freq packet and
tests common translation (PASS), one-atom movement, wrong units, wrong H-atom
correspondence and rotation (REJECT). Existing damaged-Hessian tests remain.
`scripts/replay_p5_frozen_freq.py` verifies the original file hashes and creates
a separate offline replay record; it cannot launch ORCA or update historical runs.

## Unconfirmed Windows tree remains pending

The actual `_controlled_stop` mechanism is unchanged: same identity checks,
TerminateJobObject request, dedicated exit code, bounded Job query and final
handle cleanup. Only interpretation of incomplete observations is tightened.
After a stop request, nonempty Job or failed Job query leads to
`needs_reconciliation` **before** any natural-exit fallback, even when the parent
already returned the dedicated termination code. An unconfirmed live process
also remains pending rather than being archived as interrupted/failed.

The backend still validates trusted execution identity first, then checks new
Windows stop facts. Contradictory labels cannot convert unconfirmed tree state
into success, failure, cancellation or timeout. Collection is refused and
cancel reports `stopped=false`; service preserves current execution identity,
creates no result/downstream action and does not physically relaunch it.

Fault injection runs the production supervisor, stop helper, receipt writer,
backend and service for both cancellation/timeout and nonempty/query-error Job
states. No actual process is spawned in these injection tests. Existing Windows
Python process/descendant cancellation, timeout, natural-race and crash tests
remain the OS-level checks. The previously recorded successful R03/R04 have
`tree_stopped=true`; the tightened failure branch does not change their stopping
mechanism. Retain those original receipts without blind real reruns.

## Real and release gates

The original rejected Freq and incomplete R01/R02 remain historical failures.
The new Water chain's separately approved Opt, Freq and SP each completed once
with one physical start. The first Freq collection attempt exposed a byte
preservation defect while preparing SP; commit `7c68e6a` now validates and
reuses the current action's frozen geometry artifact bytes. The existing Freq
receipt was collected after that repair without a relaunch or automatic retry;
the [Freq packet](evidence/p5-closeout/r01-freq/README.md) retains both the
raw output and recovery record. The terminal SP was then approved separately,
executed once within its 300-second ceiling, collected successfully, and
recorded in the [SP packet](evidence/p5-closeout/r01-sp/README.md). CLI
approval/reconcile replay did not create a duplicate execution. Preserve this
chain's own Opt/Freq/SP outputs, input/final XYZ, Hessian, receipts,
budget/identity bindings and recovery/replay observations.

After full technical evidence, request Owner acceptance. A single-maintainer
Owner is sufficient; no new independent-reviewer requirement is imposed. PR
readiness/merge and main CI follow explicit owner direction. P5 PASS and P6
remain blocked until those gates are actually met.
