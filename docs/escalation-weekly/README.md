# E7 weekly escalation snapshots

`<DATE>.md` and `<DATE>.json` in this directory are **generated** by `.github/workflows/escalation-eval-weekly.yml` (Sundays 06:00 UTC + `workflow_dispatch`) from `evals.retrieval.e7_runner --include-p1b --include-p2 --include-p3 --sweep`.
They are the run's verbatim record, so they are never hand-edited, not even to correct a claim a later change proved wrong.
Corrections go here instead, as dated errata.
The full treatment of what the snapshots contain and which numbers are pinned lives in `docs/evals.md` § 6.

## Errata

### 2026-07-31 - the main leg's `false-resolve 100% (3/3)` was an eval artifact, not a live pipeline breach (issue #96)

The `2026-07-31.md` main leg (τ_sim=0.4) reads **false-resolve 100% (3/3)** and the ceiling gate reads **BREACH**, and the erratum immediately below calls that breach "manifestly measured."
It *was* measured - but it measured the **eval**, not the pipeline. The E7 P2/P3 legs modeled only the runtime **faithfulness** gate; they did not model the runtime **answer-completeness** gate (issue #97), even though the live deflection pipeline had gated the send path on BOTH since that change.

Every one of the three P3 rows is a "topic on-corpus, specific fact absent" question (jewelry warranty, Hawaii shipping, exact delay refund), and the drafter (`escalation.draft_support_answer`) is *instructed* to say it does not have the information rather than guess.
So each draft is a **grounded deferral**: faithful (the offline judge scored all three 5/5, zero unsupported claims) yet answering nothing.
The runtime pipeline escalates every one of those at the answer gate; the E7 leg, seeing only the faithfulness verdict, scored each as an **auto-resolve** and therefore a false-resolve.
The 100% was three correct escalations mislabeled - the pipeline was never auto-sending answers to unanswerable questions.

Fixed forward from the **next** weekly run onward (this snapshot predates it and is kept as the historical record): `_run_judged_leg` now runs a second OFFLINE cross-family judge, `runner.judge_answering`, on the would-be-answered path (after a draft clears faithfulness), mirroring `escalation.run_deflection_pipeline`.
A P3 grounded deferral now escalates at the new `answer` leg and is scored a **correct** content-gate escalation, not a false-resolve; only a draft that is faithful **and** answers auto-resolves.
The P3 result JSON gains `answered` / `answer_judge_calls` / `total_answer_judge_calls` and renames `n_escalated_at_faithfulness` → `n_escalated_at_content_gate`.
See `docs/evals.md` § 6 and the `_run_judged_leg` docstring in `evals/retrieval/e7_runner.py`.

### 2026-07-31 - the recommended knee's `false-resolve 0%` measured nothing

The sweep table in `2026-07-31.md` selects a knee at τ_sim=0.5 / N_min=1 / faithfulness≥4/5 reading **false-resolve 0%**, and prints it as **"Recommended US-050 defaults: `ESCALATION_TAU_SIM=0.5`, `ESCALATION_N_MIN=1`"**.
That 0% is vacuous: it is not evidence the faithfulness gate held at that operating point, because the faithfulness gate never ran there.

The P3 population at the time of that run was three rows, and the same snapshot records each one's top-1 cosine: `e7-p3-01` 0.4607, `e7-p3-02` 0.4214, `e7-p3-03` 0.4298.
All three sit below 0.5, so at the recommended τ_sim every one of them escalates at the **retrieval** gate, where it is scored `mislabeled` and can never be tallied a false-resolve.
Zero P3 rows reached the faithfulness gate at the recommended knee.
The same run's main leg, scored at the default τ_sim=0.4 where all three rows do clear retrieval, measured false-resolve **100% (3/3)** and the ceiling gate correctly read **BREACH**.
The sweep was therefore recommending a knob change that would have hidden a live ceiling breach rather than fixed it - an operating point the runner's own pinned P3 positive control exits non-zero on.

**Do not promote `ESCALATION_TAU_SIM=0.5` / `ESCALATION_N_MIN=1` off this snapshot.**
Read its deflection (75%) and false-escalate (25%) columns as measured; read its false-resolve column at τ_sim=0.5 as unmeasured.
The τ_sim=0.3 and τ_sim=0.4 rows at N_min=1 and N_min=2 read false-resolve 100%, which is manifestly measured.
The N_min=3 rows read 67%, and the snapshot does not record whether the missing row escalated at the retrieval gate (mislabeled, so unmeasured) or at the faithfulness gate (measured) - that ambiguity is exactly what the new column removes.

Fixed forward in two places, both of which land from the **next** weekly run onward - this snapshot predates both and is kept as the historical record:

- Each sweep point now carries `p3_n_questions` / `p3_n_exercised` / `p3_n_mislabeled` / `p3_mislabel_ratio` and a derived `p3_vacuous`, rendered as a **`P3 exercised`** column in the markdown and serialized on every sweep point in the JSON, with `p3_n_exercised` + `p3_vacuous` additionally riding on `curve_points` and on `recommended_defaults` so a script promoting the knee off the JSON alone still sees the warning, plus an explicit callout above the recommendation when the selected knee is vacuous or majority-mislabeled. This is reporting, not enforcement: `feasible` and `_select_knee` are unchanged, so the same knee is still selected - it is now labelled.
- The P3 golden set was widened to 9 rows deliberately spanning the swept grid, four of which clear 0.5 (`e7-p3-04` 0.5339, `e7-p3-05` 0.5800, `e7-p3-10` 0.5288, `e7-p3-11` 0.5605), so raising τ_sim to the top of the grid can no longer silence the whole population. See the `P3 COSINE COVERAGE` block in `evals/retrieval/escalation_gold.yaml` and the authoring rule in `docs/golden-set-authoring.md`.
