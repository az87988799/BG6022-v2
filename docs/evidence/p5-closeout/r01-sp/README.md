# Water R01 SP evidence

This packet records the separately approved terminal SP action of the new
Water `R01/R02` chain. Opt, Freq and SP each completed once; this packet does
not constitute Owner acceptance or a scientific assessment.

- Run: `run_3e96adc04ed048f2aefee660908eaab2`
- Execution: `execution_b2545f5127a94985886ef48539946834`
- Job: `job_e5643cb0860344d5a7123d42372ae608`
- Action: `action_782d990dee74468da38bbe7cacfe4bc9`
- ORCA: `E:\\orca\\orca.exe`, 6.1.1, SHA-256
  `8d6b51bf4093c967dbed997cc651f0212b8f94313ee77ea56f548f000672c42f`
- Binding: `27e13fb3f01de00312848aa6cf221401d3f29b4f359e89f7a3b3c0c0ce365b2e`
- Budget: 1 core / 2048 MB / 300 seconds; no automatic retry.
- Result: normal exit 0, physical start count 1, parser `complete`, SCF
  converged, finite energy `-76.418938721035 Eh`.

The SP input uses the exact frozen optimized geometry bytes from the preceding
Opt action (`581b0cbfa31faf1d014c586dbb21c4fea5c791a328f963115a0a2a0c4014cca7`).
The packet preserves the SP input, geometry, stdout/stderr, terminal receipt,
execution export and read-only collector output. `manifest.json` records byte
hashes and sizes for every packet file.

Approval was performed in a fresh CLI process and the saved approval request
was replayed in another fresh CLI process. The replay returned the original
accepted event; the final state remained revision 25 with three jobs and three
results, and no duplicate SP execution was created. Dispatch and collection
were separate worker invocations; no automatic retry was used.

The complete chain is now technically complete, but `scientific_assessment`
remains `not_evaluated` and `claim_status` remains `not_generated`. P5 remains
`PENDING_OWNER_ACCEPTANCE`; P6 must not start before Owner acceptance and the
remaining release gates.
