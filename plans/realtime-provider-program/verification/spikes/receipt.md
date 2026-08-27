# Required spike receipt

Captured 2026-08-28. Spike code is disposable plan evidence, not business implementation.

## SP-SDK-MATRIX — PASS with one explicitly deferred artifact assertion

- Python 3.11.15 fresh venv installed `simple-harness-sdk==0.6.2` and `simple-harness-memory-sdk==0.5.2` from the Simple Harness vendored wheels.
- Python 3.13.13 fresh venv installed the same pair.
- In both environments, service-sdk current source ran every non-BOM test: **104 passed** in 9.09/9.08 seconds and imported successfully.
- The initial 114-test run in each environment produced **113 passed, 1 expected spike-only failure**: `test_compatibility_bom_and_installed_harness_provenance` requires installed service distribution metadata and the old 0.2.3/0.5.2/0.5.1 BOM. The 0.3.0 candidate must update that BOM and rerun this assertion from the installed immutable wheel; the spike does not claim artifact/provenance admission.

Raw terminal markers:

```text
MATRIX_PASS 3.11.15 0.6.2 0.5.2 simple_harness_service
MATRIX_PASS 3.13.13 0.6.2 0.5.2 simple_harness_service
```

Decision: the proposed source/API matrix is viable. S1 remains blocked from release until clean installed-wheel BOM/provenance passes on both versions.

## SP-FX-ATOMS — PASS

Command: `node plans/realtime-provider-program/verification/spikes/fx_atoms_spike.mjs`

```json
{"status":"SP-FX-ATOMS-PASS","fixtureRevision":{"id":"tokenseller-qwen35-usd-v1","usdAtomsPerCnyMinor":"14880267833","marginBps":"1000"},"completed":{"baseCostAtoms":"1900954216","exactChargeAtoms":"2091049638"},"cancelled":{"baseCostAtoms":"569319048","exactChargeAtoms":"626250953"},"holdMinor":"23"}
```

The revision derives its integer cross-rate from the ECB 2026-08-27 reference values EUR/USD=1.1645 and EUR/CNY=7.8258, then ceilings once to `14880267833` USD atoms per CNY minor. It proves integer-only aggregate conversion, aggregate margin rounding, and a conservative whole-cent hold. S2 must persist and bind these exact revision bytes; runtime IEEE-754 money arithmetic is forbidden.

## SP-BARGE-OVERLAP — PASS

Command: `python3 plans/realtime-provider-program/verification/spikes/barge_overlap_spike.py`

```text
SP-BARGE-OVERLAP-PASS permutations=45 cancel_no_active=pass
```

The disposable reducer covers all 45 legal permutations of speech-start, new commit, new response binding, old terminal, duplicate terminal, and late old audio under the required partial orders. It preserves at most two live turn records, one successor hold/binding, one predecessor settlement, and zero forwarded late old audio. S2 must port this oracle to the production TypeScript state machine and still capture one real metadata-only Qwen barge-in trace; the architecture no longer depends on the real trace producing one particular order.
