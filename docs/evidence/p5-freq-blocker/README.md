# Retained R01/R02 Freq blocker

Run `run_5593f377497d43fab2e25ecce0b0a44c`, execution
`execution_9ee2875accba4a10b5b9db9d7d41a72d`, implementation `c7cd1ea`.
ORCA 6.1.1 completed the Freq process with exit code 0. Application collection
rejected it with `geometry_binding_mismatch: Hessian Bohr coordinates differ
from input Angstrom geometry`. The preceding Opt parsed successfully; the SP
node was never started. This is **not** an R01 or R02 PASS.

`hessian.txt` is a byte-preserving copy of the original `input.hess` (renamed to
avoid the repository's scratch-file ignore rule). All other files retain their
original names. `evidence.json` is the original read-only collection of the run,
including the rejected result. `manifest.json` hashes this small evidence packet.
No database, binary checkpoint, scratch directory or executable is included.

The original XYZ and Hessian coordinates differ in absolute frame. A possible
frame translation needs separate verification; C1–C3 did not loosen this check
or automatically restart/rewrite the failed chain. Scientific assessment remains
`not_evaluated`; owner acceptance and P6 remain blocked.
