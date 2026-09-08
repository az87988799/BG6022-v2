# BG6022 P6 Scientific Report

- P6 run: `run_963622492d6c4e2fa3756367c6fd6a9a`
- Source P5 run: `run_3e96adc04ed048f2aefee660908eaab2`
- Source origin: `orca_local`; processing: `offline_reparse`

## Conclusion

- Minimum status: **supported_within_policy**
- all_vibrational_candidates_above_policy_floor
- This is method- and policy-limited local-minimum support; it does not establish a global minimum or thermodynamic stability.

## Method and scope

- Method: `r²SCAN-3c`
- ORCA version: `6.1.1`
- Identity: `O` (H2O)
- Charge / multiplicity: `0 / 1`
- Environment: `gas`
- No LLM, network request, ORCA launch, geometry optimization, or automatic retry was performed by P6.

## P5 result observations

| Primitive | Result | Electronic energy (Eh) | Frequency count | Parse status |
| --- | --- | ---: | ---: | --- |
| `opt` | `workflow_b98d66696b69443296f3bc868bd2ed56` | -76.418938721015 | 0 | `complete` |
| `freq` | `workflow_4d3c7a401cbc43ac89af3d22cfeb1c38` | -76.418938721035 | 9 | `complete` |
| `sp` | `workflow_a3f407d2c2eb4b0faeb786bc4c26a3b6` | -76.418938721035 | 0 | `complete` |

## Minimum checks

- `opt_converged`: passed — Opt convergence is required
- `freq_complete`: passed — Freq output and Hessian are complete
- `geometry_binding`: passed — Freq uses the bound optimized geometry
- `method_scope`: passed — Method and ORCA version are in policy
- `state_scope`: passed — Charge, multiplicity and gas environment are in policy
- `hessian`: passed — Hessian layout is complete
- `mode_layout`: passed — Projected rigid and vibrational mode layout is valid

## Vibrational modes

| Index | Frequency (cm^-1) | Kind |
| ---: | ---: | --- |
| 0 | 0.000000000000 | `projected_rigid` |
| 1 | 0.000000000000 | `projected_rigid` |
| 2 | 0.000000000000 | `projected_rigid` |
| 3 | 0.000000000000 | `projected_rigid` |
| 4 | 0.000000000000 | `projected_rigid` |
| 5 | 0.000000000000 | `projected_rigid` |
| 6 | 1653.248845317112 | `vibrational_candidate` |
| 7 | 3813.580815193918 | `vibrational_candidate` |
| 8 | 3932.734982130532 | `vibrational_candidate` |

## Thermochemistry context

- `temperature_K`: `298.15`
- `pressure_atm`: `1.0`
- `quasi_rrho`: `True`
- `cutoff_frequency_cm1`: `1.0`
- `frequency_scale_factor`: `1.0`
- `qrrho_reference_frequency_cm1`: `100.0`
- `standard_state`: `None`
- `symmetry_number`: `2`
- Free-energy, ZPE, enthalpy, and Gibbs claims are outside this P6 MVP.

## Claims

- `claim_65da0bdc1a824e68ac7e170bbeb4cbb4` — `electronic_energy` / `supported`: `-76.418938721015`; Evidence: `evidence_bbd63fef92f441de9e17a70c0abdfd55`
- `claim_cea64d4b04be4a1a97fc4917b6d820c0` — `electronic_energy` / `supported`: `-76.418938721035`; Evidence: `evidence_e5d43c0354424e0aa109db3fd96143c3`
- `claim_edc69928ec6b4be8a7165322cffc2121` — `vibrational_frequency` / `supported`: `1653.248845317112`; Evidence: `evidence_6e0035cebccc401984c5b8e5300b2850`
- `claim_00c88af98b804697a05d3e168dc1203c` — `vibrational_frequency` / `supported`: `3813.580815193918`; Evidence: `evidence_35d4bb62dafa41118292e0cf5e2349f1`
- `claim_58234128ccdc40b18d4f31fc52fe4a0d` — `vibrational_frequency` / `supported`: `3932.734982130532`; Evidence: `evidence_daab819d20774bcf9f73200dc87b3950`
- `claim_4e551c0d305f499eb87e222936dd4bf5` — `electronic_energy` / `supported`: `-76.418938721035`; Evidence: `evidence_3533d4ccb82e44eda748ba56e6302c2a`
- `claim_71ff9ac4058f4eabbbae465884df3d3a` — `local_minimum_support` / `supported`: `supported_within_policy`; Evidence: `evidence_f1f68a1415d843fdb77e5229b440b42f`, `evidence_6e8acaa0b6dd443f94c4a4d54f0a037f`, `evidence_bbd63fef92f441de9e17a70c0abdfd55`, `evidence_e5d43c0354424e0aa109db3fd96143c3`, `evidence_0857c51089894630bdf6a4d7c80e56f7`, `evidence_83ba398d5111427fa4efe842b9da2f62`, `evidence_46392385bc9c4da992a3eb195c331f1d`, `evidence_a5dfafa31b3d44ab86aab7cccabbea64`, `evidence_9c6dd7ac79314b4783f5edb8b5180185`, `evidence_fcafaec8872745518ed1e23eb66aad13`, `evidence_6e0035cebccc401984c5b8e5300b2850`, `evidence_35d4bb62dafa41118292e0cf5e2349f1`, `evidence_daab819d20774bcf9f73200dc87b3950`, `evidence_3533d4ccb82e44eda748ba56e6302c2a`

## Evidence source locations

- `evidence_f1f68a1415d843fdb77e5229b440b42f` → artifact `artifact_67d1fb8d18515d01a492e08a68b316d2`; SHA-256 `26bdc167de35cfd84a5983e1c3e3f664962f9c6fb53decaf6e83ad324066311f`; locator `{"artifact_hash": "26bdc167de35cfd84a5983e1c3e3f664962f9c6fb53decaf6e83ad324066311f", "artifact_id": "artifact_67d1fb8d18515d01a492e08a68b316d2", "block": "p5.execution_binding.input_manifest", "index": null, "line": null, "utf8_character_span": null}`
- `evidence_6e8acaa0b6dd443f94c4a4d54f0a037f` → artifact `artifact_fa3c4109ec43582194f9bcabcde98db1`; SHA-256 `8841b7214aed1a7bd8c12cefa1a39f5601c9d65a4abe5c1663f08b1243f17212`; locator `{"artifact_hash": "8841b7214aed1a7bd8c12cefa1a39f5601c9d65a4abe5c1663f08b1243f17212", "artifact_id": "artifact_fa3c4109ec43582194f9bcabcde98db1", "block": "THERMOCHEMISTRY", "index": null, "line": null, "utf8_character_span": null}`
- `evidence_bbd63fef92f441de9e17a70c0abdfd55` → artifact `artifact_0e875611883157a888c0eda449a572c6`; SHA-256 `7e3cdf0829baec6c3df32ce21c3e2c5124177407f49be0a3c23042c9d4e2349e`; locator `{"artifact_hash": "7e3cdf0829baec6c3df32ce21c3e2c5124177407f49be0a3c23042c9d4e2349e", "artifact_id": "artifact_0e875611883157a888c0eda449a572c6", "block": "FINAL SINGLE POINT ENERGY", "index": null, "line": null, "utf8_character_span": [129032, 129048]}`
- `evidence_e5d43c0354424e0aa109db3fd96143c3` → artifact `artifact_fa3c4109ec43582194f9bcabcde98db1`; SHA-256 `8841b7214aed1a7bd8c12cefa1a39f5601c9d65a4abe5c1663f08b1243f17212`; locator `{"artifact_hash": "8841b7214aed1a7bd8c12cefa1a39f5601c9d65a4abe5c1663f08b1243f17212", "artifact_id": "artifact_fa3c4109ec43582194f9bcabcde98db1", "block": "FINAL SINGLE POINT ENERGY", "index": null, "line": null, "utf8_character_span": [43083, 43099]}`
- `evidence_0857c51089894630bdf6a4d7c80e56f7` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 0, "line": null, "utf8_character_span": [2056, 2074]}`
- `evidence_83ba398d5111427fa4efe842b9da2f62` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 1, "line": null, "utf8_character_span": [2088, 2106]}`
- `evidence_46392385bc9c4da992a3eb195c331f1d` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 2, "line": null, "utf8_character_span": [2120, 2138]}`
- `evidence_a5dfafa31b3d44ab86aab7cccabbea64` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 3, "line": null, "utf8_character_span": [2152, 2170]}`
- `evidence_9c6dd7ac79314b4783f5edb8b5180185` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 4, "line": null, "utf8_character_span": [2184, 2202]}`
- `evidence_fcafaec8872745518ed1e23eb66aad13` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 5, "line": null, "utf8_character_span": [2216, 2234]}`
- `evidence_6e0035cebccc401984c5b8e5300b2850` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 6, "line": null, "utf8_character_span": [2245, 2266]}`
- `evidence_35d4bb62dafa41118292e0cf5e2349f1` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 7, "line": null, "utf8_character_span": [2277, 2298]}`
- `evidence_daab819d20774bcf9f73200dc87b3950` → artifact `artifact_993f97a944bd56ada1a48ab356ce3911`; SHA-256 `de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97`; locator `{"artifact_hash": "de987e2af81d9ac9a2fc0463a23adcbe7d31eb45434e915ffb2300f061696f97", "artifact_id": "artifact_993f97a944bd56ada1a48ab356ce3911", "block": "vibrational_frequencies", "index": 8, "line": null, "utf8_character_span": [2309, 2330]}`
- `evidence_3533d4ccb82e44eda748ba56e6302c2a` → artifact `artifact_dd2b236dd2e0556d8e7ae6c81576960c`; SHA-256 `8978cbcba68405544b5894c0e00d15d4c546160670d64622c4e72fbd1ac5c9d1`; locator `{"artifact_hash": "8978cbcba68405544b5894c0e00d15d4c546160670d64622c4e72fbd1ac5c9d1", "artifact_id": "artifact_dd2b236dd2e0556d8e7ae6c81576960c", "block": "FINAL SINGLE POINT ENERGY", "index": null, "line": null, "utf8_character_span": [40224, 40240]}`

## Limitations

- single starting conformer; no conformer search or global minimum search
- electronic energy only; no ZPE, enthalpy, Gibbs free energy, or thermodynamic ranking claim
- minimum support is limited to the registered method, geometry binding, and scientific policy

## Verification references

- Evidence records: 14
- Claims: 7
- Policy hash: `1f9ea2c04874b7de843014ad86cbdef0d78ff73e35bb9dd16b0860b93db6b561`
- Source snapshot hash: `06ce9a9e189d7f6c6629dc251fd3b9b0578f331425a5c94dbc51faf3a59b1c61`
