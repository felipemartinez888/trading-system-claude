# D-06: open interface questions for the evaluation protocol

Status: **questions only.** D-06 approved coordinating the A/B protocol with whoever operates the
other system. The format (`tsc-eval`, [EVALUATION_PROTOCOL](EVALUATION_PROTOCOL.md) §4–§5) is a
DRAFT and is not agreed. Nothing here contacts the other team or uses their code or data
(CLAUDE.md rule 1). The owner relays the questions and records the answers.

What exists today (TSC side):
- JSON Lines export with JSON Schema 2020-12 files;
- canonical JSON, line hashes, and the source events' hash chain;
- a manifest with file hashes;
- a verifier, and a standard-library scorer that reproduces the dashboard's paper-book and signal
  values from the export alone. It was verified on three real recorded sessions: 9 of 9 ledger
  hashes reproduced.

| # | Question | Why it matters | TSC proposal |
|---|---|---|---|
| Q1 | Can the other system produce these files, or do we agree on a smaller common subset? | Its records may not map one to one | A **common core**: `signals`, `transitions`, `fills`, `positions`, `coverage`, `manifest`. The TSC-only files are optional for them: `steps`, `orders`, `ledger`, `sim_events`, `agents` |
| Q2 | Integrity fields: does their system keep a hash-chained event log? | Without one, `event.hash` and `prev_hash` cannot be filled | For them, line hashes and file hashes only. Chain verification is reported as "not available", never as passed |
| Q3 | Numbers: money as plain decimal strings, bar prices as JSON numbers read through their decimal text? | Exact reproduction of ledger values | Keep as is |
| Q4 | Time: UTC epoch milliseconds plus ISO-8601 `Z`? | Matching signals across systems | Keep as is |
| Q5 | Provenance: how do their data sources map to the ADR-0010 labels (LIVE, RECOVERED, HISTORICAL, SIMULATED, …)? | Demo or backfilled data must never be compared as live | They supply a mapping table, frozen with the protocol |
| Q6 | Matched view: the tolerance window for "same symbol and side" signals | Defines the paired comparison (§5) | One strategy bar either side of the decision bar's close; to be agreed |
| Q7 | Metrics not yet defined: block length of the bootstrap, Sharpe/Sortino annualization, what counts as an "opportunity" for the abstention rate, regime labels | The scorer does not compute these yet (`PROTOCOL_METRICS_NOT_SCORED`) | Agree the definitions before any comparison is run, and freeze them |
| Q8 | Advisory agents (format v2 on `task/m5-runtime-wiring`): should agent reviews be part of the shared format? | TSC's `agent_gated` book is an A/B of its own | An optional, advisory-only `agents.jsonl`. Compare agent value only TSC-internally (`agent_gated` vs `baseline`) |
| Q9 | Versioning: how are format changes agreed, and how long are old versions accepted? | v2 exists already, and v1 exports remain checkable | Additive changes bump the version. Each version keeps its own schema directory, and the scorer reads every listed version |
| Q10 | Freezing: the protocol document's hash in every manifest (`protocol.sha256`), and from when | Shows that predictions preceded outcomes | Freeze before the forward trial (M7). Daily chain-head commits from then on |
| Q11 | Scorer: one shared scorer (TSC's standard-library file), each team's own, or both with a cross-check? | Trust in the result | Both: each team scores both exports, and the results must agree |
| Q12 | Delivery: how exports are exchanged (files, a repository, a shared drive), and how often | Operational | A zip of the export folder with its SHA-256, per agreed period |

When the answers are in, the format is frozen as a new version, its hash goes into every manifest,
and the scorer tests pin it.
