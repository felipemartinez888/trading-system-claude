# M5-3 runtime decisions (for the owner to fill in)

Status: **blank. Nothing here is decided.** The work is on the separate branch
`task/m5-runtime-wiring`, which is **not merged**: merging is D-H. Mock clients only. Agent mode
`off` is the default and leaves Paper byte-identical. Agents advise only (ADR-0007). Details and
tests: `docs/devlog/2026-11-10-m5-runtime-wiring.md` on that branch.

To decide, reply with the ids and letters, for example: `M5-Q1 a; M5-Q2 a; M5-Q3 a; M5-Q4 a`.

## M5-Q1: changing the agent mode needs a new paper session
- **a. (recommended, as built)** A session records its mode in its configuration. Switching `off`
  ↔ `confirmed` on an existing session makes its restore fail (`UNAVAILABLE`), as a `risk.yaml`
  change does. Start a new session to switch. The A/B comparison stays fair: `agent_gated` and
  `baseline` start together with the same equity.
- **b.** Let `agent_gated` start mid-session with fresh equity. The books then cover different
  periods, which weakens the comparison.
- Decision: ______

## M5-Q2: reviews in flight at a restart
- **a. (recommended, as built)** Record them as `AI_UNAVAILABLE` ("review interrupted"). They are
  never re-requested and never approved late. `agent_gated` does not take those signals.
- **b.** Re-request them after the restart. The cache may already hold a finished review, but
  budget would be spent on signals that have usually expired by then.
- Decision: ______

## M5-Q3: one review worker, outcomes in request order
- Facts:
  - one worker;
  - outcomes are journaled in request order, which replay requires;
  - a slow review delays the journaling of later outcomes;
  - with several confirmed signals in one minute, later ones can expire before their review.
- **a. (recommended)** Keep one ordered worker, and report the coverage this costs separately:
  signals whose validity ended while waiting behind another review, as a distinct count. The
  admission rule does not change.
- **b.** Keep one ordered worker; no separate count (today's `coverage.by_state` lumps it in).
- **c.** Several workers. This needs a new replay-ordering rule; not proposed for the MVP.
- Decision: ______

## M5-Q4: what DOWNGRADED means for `agent_gated`
- **a. (recommended, as built)** DOWNGRADED blocks admission, as VETOED does. It never resizes,
  reprices or re-times anything. The baseline is unaffected.
- **b.** Admit DOWNGRADED signals at a lower priority, for example never when the book is at its
  position limit. This is a new rule and would have to be preregistered before any A/B result.
- Decision: ______

## Found and fixed on the branch in this session (no decision needed)
- **No `agent_gated` session could be exported** to the M6 evaluation format. The agent events sit
  inside paper batches, and the exporter refused them. It failed closed.
- **Fixed by export format v2:**
  - a new `agents.jsonl` file, with a schema that refuses any line claiming to affect the baseline;
  - v1 exports are still checked and scored.
- **Evidence:**
  - four outcome mixes export, verify and score, with all four ledger hashes reproduced;
  - the `9c681ca` v1 exporter and the v2 exporter give byte-identical v1 output on one store.
- This adds one question to **D-06**: should agent events be part of the shared format, as a
  separate advisory-only file?

## Before a merge (D-H)
1. The branch's CI is green on its final commit.
2. Answers to M5-Q1 to Q4. Any answer other than the built option is implemented and reviewed
   first.
3. Mode-off identity is re-checked after the merge. The golden values in
   `test_agents_paper_wiring.py` hold the 9c681ca session bytes.
4. The D-A replay evidence is unaffected: replay the D-A sessions with `agents.yaml` mode `off`
   (L-13).

Out of scope until separately approved:
- the real model adapter;
- any API key;
- any paid call.
