# c7cd1ea completion review: minimal C1–C3 repair

Status: implemented, verification in progress; **PENDING_OWNER_ACCEPTANCE**.
No replacement of shared worker, schema, ticket, compiler or historical migrations.

## C1 — bound calculation segment

The parser first requires one normal termination marker and bounds all SCF and
energy extraction before it. Only whitespace and the standard total-runtime
summary are permitted after it; extra energy, banners and contradictory error
termination are rejected. Each energy requires an explicit preceding SCF success
in its own segment; `SCF CONVERGENCE` headings do not prove success. Genuine CRLF
multi-cycle output still parses. Parser contract is now `orca-parser-v3`.

## C2 — optimized identity compatibility

Before an optimized GeometryRecord or downstream action is created, actual
archived XYZ is checked against the explicit-H confirmed atom ordering and
connectivity. A general covalent-radius screen rejects changed or borderline
connections and overlaps. Bonded distances must be 0.55–1.25 times the sum of
element covalent radii; nonbonded distances below 1.35 times that sum are rejected
as incompatible/ambiguous. This deliberately conservative guard does not infer
new bond orders or certify a minimum. Ambiguity blocks continuation.

RDKit reassigns 3D tetrahedral and double-bond stereo on the confirmed graph,
without inherited tags, and compares explicitly confirmed CIP and E/Z labels.
Unrecoverable or changed explicit stereo fails closed. The original files remain
archived and a rejected result is recorded; no Freq action inherits the identity.
See [RDKit 3D stereochemistry API](https://www.rdkit.org/docs/source/rdkit.Chem.rdmolops.html).

## C3 — observed stop, not requested stop

Runner polls natural exit before evaluating control conditions, then the stop
helper rechecks both creation identity and natural exit immediately before
requesting control. The receipt records request-time identity/liveness, OS
request, observed exit, confirmation times, and Windows Job Object active count.
A dedicated termination code connects the observed exit to this stop operation;
an arbitrary nonzero return code is not sufficient. A racing natural exit keeps
its natural outcome. OS errors or unavailable identity cannot generate PASS.

Real R03/R04 classification requires complete internally matching stop facts and
an empty Windows Job Object, not merely `cancelled`/`timed_out` or a start count.
Missing old facts or no physical start is NOT_EXERCISED; contradictory/unconfirmed
requested control is FAIL. Old R03 is therefore not independently accepted under
the repaired rule. POSIX controlled tests do not claim Windows tree-empty proof.

## Regression map and limits

- `tests/p5/test_p5_completion_review.py`: real output suffix corruption; SCF
  heading/prior-cycle rejection; water/ethanol/benzene/chiral/E-Z identity checks;
  broken Opt business flow; mirrored stereo; changed E/Z; already-exited,
  pre-request and during-request natural exit; real exited Python child; unknown
  creation identity; gate rejects incomplete control facts.
- Existing genuine CRLF fixture and all P5 controlled process tests retained.
- Reviewer Linux identity differences remain environment-limited, not reproduced
  here: `/proc` disappearance, denied access, missing visibility or zombie state
  all yield unknown creation identity. No PID-only fallback or Linux production
  expansion is introduced. Prior GitHub Ubuntu jobs passed; this does not erase
  the reviewer's isolated-environment failures.

## Real execution history — not rewritten

The first Opt was rejected by the former CRLF parser. The later authorized run
`run_5593f377497d43fab2e25ecce0b0a44c` completed Opt, then its real Freq output
was rejected because Hessian coordinates did not match the frozen XYZ in the
same absolute frame. It did not run SP or pass R01/R02. Raw Hessian suggests a
coordinate-frame translation; that is a separate output-contract investigation,
not silently relaxed as part of C1–C3. No failed computation is auto-retried.

Previously authorized, unused 3-second timeout and 30-second cancellation tests
may be executed only once each after these checks. R01/R02 remain blocked on the
separate Freq failure. `P5_ACCEPTANCE.md` records final numbers and evidence.
