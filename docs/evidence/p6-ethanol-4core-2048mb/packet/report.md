# BG6022 P6 Scientific Report

- P6 run: `run_215f4cad3ad442a7a0e6fc182d8a851e`
- Source P5 run: `run_31b945eddf7e4cfdb823daba41c1e38e`
- Source origin: `orca_local`; processing: `offline_reparse`

## Conclusion

- Minimum status: **supported_within_policy**
- all_vibrational_candidates_above_policy_floor
- This is method- and policy-limited local-minimum support; it does not establish a global minimum or thermodynamic stability.

## Method and scope

- Method: `r²SCAN-3c`
- ORCA version: `6.1.1`
- Identity: `CCO` (C2H6O)
- Charge / multiplicity: `0 / 1`
- Environment: `gas`
- No LLM, network request, ORCA launch, geometry optimization, or automatic retry was performed by P6.

## P5 result observations

| Primitive | Result | Electronic energy (Eh) | Frequency count | Parse status |
| --- | --- | ---: | ---: | --- |
| `opt` | `workflow_fcb30613e1a24aac928c679b4c6283d2` | -155.002314096955 | 0 | `complete` |
| `freq` | `workflow_39c9ea6aadc647d1aebb29643d195d52` | -155.002314094574 | 27 | `complete` |
| `sp` | `workflow_3396867d4db34dd1b02c11ffd9d69467` | -155.002314094574 | 0 | `complete` |

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
| 6 | 248.133764970298 | `vibrational_candidate` |
| 7 | 298.543850583276 | `vibrational_candidate` |
| 8 | 423.179388886663 | `vibrational_candidate` |
| 9 | 826.877314378680 | `vibrational_candidate` |
| 10 | 895.635189081537 | `vibrational_candidate` |
| 11 | 1029.276400771195 | `vibrational_candidate` |
| 12 | 1096.363228021474 | `vibrational_candidate` |
| 13 | 1178.070322135833 | `vibrational_candidate` |
| 14 | 1281.963748429600 | `vibrational_candidate` |
| 15 | 1297.681961804003 | `vibrational_candidate` |
| 16 | 1393.285263618188 | `vibrational_candidate` |
| 17 | 1446.350277679499 | `vibrational_candidate` |
| 18 | 1476.655237009214 | `vibrational_candidate` |
| 19 | 1495.069743705402 | `vibrational_candidate` |
| 20 | 1520.655006781502 | `vibrational_candidate` |
| 21 | 2954.372493119408 | `vibrational_candidate` |
| 22 | 2990.947765560973 | `vibrational_candidate` |
| 23 | 3020.481558783774 | `vibrational_candidate` |
| 24 | 3109.109646880243 | `vibrational_candidate` |
| 25 | 3111.438271139069 | `vibrational_candidate` |
| 26 | 3837.522071531320 | `vibrational_candidate` |

## Thermochemistry context

- `temperature_K`: `298.15`
- `pressure_atm`: `1.0`
- `quasi_rrho`: `True`
- `cutoff_frequency_cm1`: `1.0`
- `frequency_scale_factor`: `1.0`
- `qrrho_reference_frequency_cm1`: `100.0`
- `standard_state`: `None`
- `symmetry_number`: `1`
- Free-energy, ZPE, enthalpy, and Gibbs claims are outside this P6 MVP.

## Claims

- `claim_f665c4264e4c45e3a06e382405cf2232` — `electronic_energy` / `supported`: `-155.002314096955`; Evidence: `evidence_286fd1576cf24d858c0b434c0542d3c0`
- `claim_bdf2c892e969403a87defac1c93c0c8d` — `electronic_energy` / `supported`: `-155.002314094574`; Evidence: `evidence_d723468263764b23b1a81b41bc589dd2`
- `claim_166e617bac044d25a8872ee89686ffaa` — `vibrational_frequency` / `supported`: `248.13376497029768`; Evidence: `evidence_3186b829ab2244e8a703ba6f75ea8c2c`
- `claim_70ad6b3b424443c1a7891a10f0f5d58d` — `vibrational_frequency` / `supported`: `298.543850583276`; Evidence: `evidence_fd9981291cee463e9e8d7ebd424a7afa`
- `claim_84f2dc71b585435786730aa83b9c5412` — `vibrational_frequency` / `supported`: `423.1793888866634`; Evidence: `evidence_6957cc4fd31b46dab0f489b1b88d3ead`
- `claim_d4543c5d13ae4a39b7b931d5155ce56a` — `vibrational_frequency` / `supported`: `826.8773143786797`; Evidence: `evidence_090bd8323ed54701ad3787313d1e2844`
- `claim_ba8b7ddada6d4f3bb36f84fd14e191ef` — `vibrational_frequency` / `supported`: `895.6351890815366`; Evidence: `evidence_2dda9297f8104c2ca455a4aa9058d504`
- `claim_835e18b4c18b40c9a612447091b01d66` — `vibrational_frequency` / `supported`: `1029.2764007711949`; Evidence: `evidence_062e2bb4af164a019d29c8efd46acedf`
- `claim_d39021dbb3424229a604d0b84a7d4a1c` — `vibrational_frequency` / `supported`: `1096.3632280214738`; Evidence: `evidence_f8693e53c7bf4a8a9abdfdd8a3f22b71`
- `claim_55e7c0b67c374af39b43b29454232022` — `vibrational_frequency` / `supported`: `1178.0703221358328`; Evidence: `evidence_dd540d762568493e9e40809b1490a0fc`
- `claim_e8af91a4ff8947108fb5b670b0c3955b` — `vibrational_frequency` / `supported`: `1281.9637484296`; Evidence: `evidence_56984fe8e82d47a68ae2e7b7e760a270`
- `claim_3359a6d84b89419d923713a2e807a676` — `vibrational_frequency` / `supported`: `1297.6819618040033`; Evidence: `evidence_6f745995c52741b6812c737c1dd4407c`
- `claim_7fcd68bb2cbf485d8e760dfd354aa423` — `vibrational_frequency` / `supported`: `1393.2852636181883`; Evidence: `evidence_08b207b31c9d4ae99b05cc0542a6bc1f`
- `claim_ee9a5d8a53a140b89a743eb04b8e075d` — `vibrational_frequency` / `supported`: `1446.3502776794985`; Evidence: `evidence_7c525f1bf35a4374ad1e8b3d11e9eebc`
- `claim_53e47f89867745938248c20b0b8cd822` — `vibrational_frequency` / `supported`: `1476.6552370092136`; Evidence: `evidence_bf22f1230b7d41cc93ca76364441114b`
- `claim_2761e1c27b844e80982167cebbeac7b0` — `vibrational_frequency` / `supported`: `1495.0697437054016`; Evidence: `evidence_d779ce64669b44deab5a724fbf0de14e`
- `claim_d77b2667cd4040808cb06146b83fdcb0` — `vibrational_frequency` / `supported`: `1520.6550067815017`; Evidence: `evidence_2b991292df3e4e738cf51419248a1c46`
- `claim_93196b38559a4c5896df9bd88a9b522f` — `vibrational_frequency` / `supported`: `2954.372493119408`; Evidence: `evidence_9afc61c3f4b340e6ada0e76f42bba8ce`
- `claim_4ba10a57f2764d6181317aeb3ef1f79d` — `vibrational_frequency` / `supported`: `2990.9477655609726`; Evidence: `evidence_c34dc4fa35c74d8098ecbfc12bcd8834`
- `claim_9a06fc6a9a6c4e70b851a84e61a70e82` — `vibrational_frequency` / `supported`: `3020.4815587837743`; Evidence: `evidence_3dc3037f0b8c46e197970e8b3330b13a`
- `claim_a3c814ef10464923b240e52d99f3f929` — `vibrational_frequency` / `supported`: `3109.1096468802425`; Evidence: `evidence_365f565ebe1f47409642d62ab60ad875`
- `claim_5bf3670c18a24fe0a24c8d68be5a9b01` — `vibrational_frequency` / `supported`: `3111.438271139069`; Evidence: `evidence_9958abc3c263423586ab6b1ddf28ffcd`
- `claim_6c4950f3bbdc4dd5a8c41f3fb2185a5b` — `vibrational_frequency` / `supported`: `3837.52207153132`; Evidence: `evidence_521c477304bf4b3a8e509fead39b57a9`
- `claim_afbc7401b3764e60a0d483b2313aca03` — `electronic_energy` / `supported`: `-155.002314094574`; Evidence: `evidence_32852bc102ab4dfe9f49644c3a128ba0`
- `claim_827eb117cab644fd82fe179b389f1f5a` — `local_minimum_support` / `supported`: `supported_within_policy`; Evidence: `evidence_09d3e0de977c454b89c803fd5810d42a`, `evidence_c7d26b0c3f6b4ff19798f42a47dc43ef`, `evidence_286fd1576cf24d858c0b434c0542d3c0`, `evidence_d723468263764b23b1a81b41bc589dd2`, `evidence_556e5b20cdf844658cfa995f673b177e`, `evidence_be9053c87b534979ab99780169262f36`, `evidence_17f8921669c84ac29e7c642f81ff386d`, `evidence_e68215919e3e4273b36ef98fbbad3608`, `evidence_7708ecd58a644e37a4294df0f4fd6216`, `evidence_2b84f6f212984346aa7eb47b12ed96ad`, `evidence_3186b829ab2244e8a703ba6f75ea8c2c`, `evidence_fd9981291cee463e9e8d7ebd424a7afa`, `evidence_6957cc4fd31b46dab0f489b1b88d3ead`, `evidence_090bd8323ed54701ad3787313d1e2844`, `evidence_2dda9297f8104c2ca455a4aa9058d504`, `evidence_062e2bb4af164a019d29c8efd46acedf`, `evidence_f8693e53c7bf4a8a9abdfdd8a3f22b71`, `evidence_dd540d762568493e9e40809b1490a0fc`, `evidence_56984fe8e82d47a68ae2e7b7e760a270`, `evidence_6f745995c52741b6812c737c1dd4407c`, `evidence_08b207b31c9d4ae99b05cc0542a6bc1f`, `evidence_7c525f1bf35a4374ad1e8b3d11e9eebc`, `evidence_bf22f1230b7d41cc93ca76364441114b`, `evidence_d779ce64669b44deab5a724fbf0de14e`, `evidence_2b991292df3e4e738cf51419248a1c46`, `evidence_9afc61c3f4b340e6ada0e76f42bba8ce`, `evidence_c34dc4fa35c74d8098ecbfc12bcd8834`, `evidence_3dc3037f0b8c46e197970e8b3330b13a`, `evidence_365f565ebe1f47409642d62ab60ad875`, `evidence_9958abc3c263423586ab6b1ddf28ffcd`, `evidence_521c477304bf4b3a8e509fead39b57a9`, `evidence_32852bc102ab4dfe9f49644c3a128ba0`

## Evidence source locations

- `evidence_09d3e0de977c454b89c803fd5810d42a` → artifact `artifact_648388f68f7d5b0a817631a2c0b60673`; SHA-256 `37b69b65237c8d92f3842adaafa88670c4701fc18b1f877cf99329fe37800025`; locator `{"artifact_hash": "37b69b65237c8d92f3842adaafa88670c4701fc18b1f877cf99329fe37800025", "artifact_id": "artifact_648388f68f7d5b0a817631a2c0b60673", "block": "p5.execution_binding.input_manifest", "index": null, "line": null, "utf8_character_span": null}`
- `evidence_c7d26b0c3f6b4ff19798f42a47dc43ef` → artifact `artifact_ac270e828bbf5030b0dc08d15382fcee`; SHA-256 `65899eec4d257545da4de3ce962624b16249d5e40961c3ba5202f9271d4e1686`; locator `{"artifact_hash": "65899eec4d257545da4de3ce962624b16249d5e40961c3ba5202f9271d4e1686", "artifact_id": "artifact_ac270e828bbf5030b0dc08d15382fcee", "block": "THERMOCHEMISTRY", "index": null, "line": null, "utf8_character_span": null}`
- `evidence_286fd1576cf24d858c0b434c0542d3c0` → artifact `artifact_edf47b360c985b9d8f8e5922f4511888`; SHA-256 `0ce3b04055042ead57947a02659677347a6559effb4dcc9b404bce52103ff171`; locator `{"artifact_hash": "0ce3b04055042ead57947a02659677347a6559effb4dcc9b404bce52103ff171", "artifact_id": "artifact_edf47b360c985b9d8f8e5922f4511888", "block": "FINAL SINGLE POINT ENERGY", "index": null, "line": null, "utf8_character_span": [333343, 333360]}`
- `evidence_d723468263764b23b1a81b41bc589dd2` → artifact `artifact_ac270e828bbf5030b0dc08d15382fcee`; SHA-256 `65899eec4d257545da4de3ce962624b16249d5e40961c3ba5202f9271d4e1686`; locator `{"artifact_hash": "65899eec4d257545da4de3ce962624b16249d5e40961c3ba5202f9271d4e1686", "artifact_id": "artifact_ac270e828bbf5030b0dc08d15382fcee", "block": "FINAL SINGLE POINT ENERGY", "index": null, "line": null, "utf8_character_span": [50257, 50274]}`
- `evidence_556e5b20cdf844658cfa995f673b177e` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 0, "line": null, "utf8_character_span": [16070, 16088]}`
- `evidence_be9053c87b534979ab99780169262f36` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 1, "line": null, "utf8_character_span": [16102, 16120]}`
- `evidence_17f8921669c84ac29e7c642f81ff386d` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 2, "line": null, "utf8_character_span": [16134, 16152]}`
- `evidence_e68215919e3e4273b36ef98fbbad3608` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 3, "line": null, "utf8_character_span": [16166, 16184]}`
- `evidence_7708ecd58a644e37a4294df0f4fd6216` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 4, "line": null, "utf8_character_span": [16198, 16216]}`
- `evidence_2b84f6f212984346aa7eb47b12ed96ad` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 5, "line": null, "utf8_character_span": [16230, 16248]}`
- `evidence_3186b829ab2244e8a703ba6f75ea8c2c` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 6, "line": null, "utf8_character_span": [16260, 16280]}`
- `evidence_fd9981291cee463e9e8d7ebd424a7afa` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 7, "line": null, "utf8_character_span": [16292, 16312]}`
- `evidence_6957cc4fd31b46dab0f489b1b88d3ead` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 8, "line": null, "utf8_character_span": [16324, 16344]}`
- `evidence_090bd8323ed54701ad3787313d1e2844` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 9, "line": null, "utf8_character_span": [16356, 16376]}`
- `evidence_2dda9297f8104c2ca455a4aa9058d504` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 10, "line": null, "utf8_character_span": [16388, 16408]}`
- `evidence_062e2bb4af164a019d29c8efd46acedf` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 11, "line": null, "utf8_character_span": [16419, 16440]}`
- `evidence_f8693e53c7bf4a8a9abdfdd8a3f22b71` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 12, "line": null, "utf8_character_span": [16451, 16472]}`
- `evidence_dd540d762568493e9e40809b1490a0fc` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 13, "line": null, "utf8_character_span": [16483, 16504]}`
- `evidence_56984fe8e82d47a68ae2e7b7e760a270` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 14, "line": null, "utf8_character_span": [16515, 16536]}`
- `evidence_6f745995c52741b6812c737c1dd4407c` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 15, "line": null, "utf8_character_span": [16547, 16568]}`
- `evidence_08b207b31c9d4ae99b05cc0542a6bc1f` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 16, "line": null, "utf8_character_span": [16579, 16600]}`
- `evidence_7c525f1bf35a4374ad1e8b3d11e9eebc` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 17, "line": null, "utf8_character_span": [16611, 16632]}`
- `evidence_bf22f1230b7d41cc93ca76364441114b` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 18, "line": null, "utf8_character_span": [16643, 16664]}`
- `evidence_d779ce64669b44deab5a724fbf0de14e` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 19, "line": null, "utf8_character_span": [16675, 16696]}`
- `evidence_2b991292df3e4e738cf51419248a1c46` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 20, "line": null, "utf8_character_span": [16707, 16728]}`
- `evidence_9afc61c3f4b340e6ada0e76f42bba8ce` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 21, "line": null, "utf8_character_span": [16739, 16760]}`
- `evidence_c34dc4fa35c74d8098ecbfc12bcd8834` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 22, "line": null, "utf8_character_span": [16771, 16792]}`
- `evidence_3dc3037f0b8c46e197970e8b3330b13a` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 23, "line": null, "utf8_character_span": [16803, 16824]}`
- `evidence_365f565ebe1f47409642d62ab60ad875` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 24, "line": null, "utf8_character_span": [16835, 16856]}`
- `evidence_9958abc3c263423586ab6b1ddf28ffcd` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 25, "line": null, "utf8_character_span": [16867, 16888]}`
- `evidence_521c477304bf4b3a8e509fead39b57a9` → artifact `artifact_2eff5ab1d5445e6e8dcccf62c08586d7`; SHA-256 `92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958`; locator `{"artifact_hash": "92502bae63b61114ec0d770d22b11d71f922e5baf094097322ec91827aa49958", "artifact_id": "artifact_2eff5ab1d5445e6e8dcccf62c08586d7", "block": "vibrational_frequencies", "index": 26, "line": null, "utf8_character_span": [16899, 16920]}`
- `evidence_32852bc102ab4dfe9f49644c3a128ba0` → artifact `artifact_666d0a917fd95a32bc08c4572f01793e`; SHA-256 `0a287d8e4a982e1629315cd7d24397786699eab6a7d7f6c0e0b926f338b3cdd2`; locator `{"artifact_hash": "0a287d8e4a982e1629315cd7d24397786699eab6a7d7f6c0e0b926f338b3cdd2", "artifact_id": "artifact_666d0a917fd95a32bc08c4572f01793e", "block": "FINAL SINGLE POINT ENERGY", "index": null, "line": null, "utf8_character_span": [47078, 47095]}`

## Limitations

- single starting conformer; no conformer search or global minimum search
- electronic energy only; no ZPE, enthalpy, Gibbs free energy, or thermodynamic ranking claim
- minimum support is limited to the registered method, geometry binding, and scientific policy

## Verification references

- Evidence records: 32
- Claims: 25
- Policy hash: `1f9ea2c04874b7de843014ad86cbdef0d78ff73e35bb9dd16b0860b93db6b561`
- Source snapshot hash: `877316aaa67cbdfd75748fd3c70c715cb858c4475f26f623d864d22ca679d0f5`
