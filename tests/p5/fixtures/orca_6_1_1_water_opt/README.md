# Real ORCA fixture provenance

This is a byte-preserving copy of the explicitly authorized Water Opt run on
2026-09-07, not synthetic data. No ORCA executable is included.

- Executable: `E:\orca\orca.exe`, Program Version **6.1.1**.
- Executable SHA-256: `8d6b51bf4093c967dbed997cc651f0212b8f94313ee77ea56f548f000672c42f`.
- Run: `run_a925255aeda74631b4f5e2877cd551c9`.
- Execution: `execution_04c3e43f6b324ff5972c5fd2ff1304e9`.
- Input: r2SCAN-3c / TightSCF / Opt, Water, 1 core, 2048 MB, 900 s ceiling.
- Actual ORCA: four optimization cycles, normal termination, exit code 0.
- Original application outcome: **rejected** by the pre-fix CRLF parser.
  The failed run is retained unchanged. The regression reads its files only;
  it does not retroactively claim the failed R01/R02 execution chain passed.
- Scientific assessment: `not_evaluated`; claim status: `not_generated`.

| File | SHA-256 |
|---|---|
| stdout.out | d05245e18d406d3d59e7a80ef607eaef889291b085cd2ba6691b17231bbd77af |
| input.xyz | b5cb73f44717bf84cb411507be4b6d9b0921814da39a2c86dcbfbc1870db8027 |
| geometry.xyz | 9ad6fbb7bcc183d55ff471d122a4551907003fd53f9060bf2b4065057cc5a433 |

`.gitattributes` preserves original CRLF bytes. Stdout includes the initial,
intermediate and final stationary-point geometries; `input.xyz` is the actual
final artifact, including its original comment and numeric precision.
