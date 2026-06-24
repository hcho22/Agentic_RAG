# PRD — Permission-Aware RAG + AI-Support Starter Kit

**Status:** consolidation of ADRs 0001–0010, SPEC.md, GAP-ANALYSIS.md, and the built Agentic RAG system into one implementable document. This document supersedes SPEC.md as the single source of truth. Terminology is governed by CONTEXT.md.

> **Phase-2 grilling reconciliation (2026-06-16).** Design sessions since this PRD was written produced the **on-disk ADR 0002** (workspace tenant isolation) in `docs/adr/`, plus **ruled Phase-2 decisions 0003** (escalation signal + deflection pipeline) and **0004** (support-conversation state + human-handoff) whose ADR files are not yet written, plus a resolved **Observability** design in `CONTEXT.md`. These **resolve** several items flagged open below: §11 is ruled (ADR-0003), and S1/S2/E7/S4/O1–O4 now have settled designs (still `TODO` to *build*, no longer `TODO (open)`). **Numbering caveat:** the Phase-2 `docs/adr/` numbering (0001 ragas, 0002 workspace, 0003 escalation, 0004 conversation; 0003/0004 ruled but not yet on disk) is **distinct from** this PRD's *internal* ADR-0001–0010 references inherited from the original design docs - see the crosswalk in §12.

**One-line product:** a production starter kit that agency/freelance developers deploy to ship a client's AI feature on a deadline — permission-aware RAG with isolation and answer quality *proven by evals*, packaged so it can be redeployed per client and demoed as either a knowledge assistant or a B2B support platform.

---

## 0. How to read this document

Every functional requirement carries a status tag:

- **`BUILT`** — exists in the current Agentic RAG codebase, may need genericization.
- **`PARTIAL`** — partly built; the gap is specified.
- **`TODO`** — not built.

and a source reference (`ADR-NNNN`, `FR-NN` from the original PRD, or `GAP` group). Implement in the order of Section 9.

---

## 1. Decisions assumed here (flag — overrule any of these)

These reconcile the ADRs against what you actually built. Each was a recommendation in GAP-ANALYSIS; none received your explicit ruling. They are baked into this PRD so it's coherent — change them and the affected sections change.

| # | Conflict | Assumed resolution | Supersedes |
|---|---|---|---|
| A1 | Sharing model vs. tenant model | **Keep the built per-document ACL model AND add a thin Workspace layer above it.** Hero = "document-level sharing *and* org-level isolation, both enforced in the retrieval predicate, both eval-proven." | Amends ADR-0002 |
| A2 | Supabase Auth vs. Clerk | **Keep Supabase Auth** (it's woven through every RLS policy; migrating = rewriting the security core). Workspaces built in-schema, not inherited from Clerk Orgs. | Supersedes ADR-0006 |
| A3 | LangSmith vs. vendor-neutral | **Hold the spec line** — self-hosted Postgres traces + OTel; LangSmith demoted to one optional exporter. | Keeps ADR-0003 |
| A4 | Vite vs. Next.js frontend | **Keep the built React/Vite frontend.** The "replaceable reference" logic makes a rewrite pointless. | Supersedes ADR-0007 |
| A5 | CI gate comment-only vs. fail-build | **Ship both as a config:** `off \| comment \| fail`, per-metric thresholds, default `comment`. | Reconciles ADR-0003 |
| A6 | Echo extras (sub-agents, text-to-SQL, web search, rerankers) | **Keep as optional modules** with documented deletion paths; quickstart runs without them. | New (D6) |

**~~Still open~~ → RESOLVED (2026-06-16):** the escalation *signal source* is ruled by **ADR-0003** (ruled; on-disk ADR file not yet written) - signal source = **3+4 as a retrieval-first cascade** (retrieval gate first, faithfulness gate only on strong retrieval). All dependent requirements (S1, S2, E7) are retagged from `TODO (open)` to `TODO` (design settled, not yet built). **A1 and A2 are likewise ruled** by on-disk ADR-0002 (workspace layer + Supabase Auth retained). A3–A6 remain assumed-unless-overruled (low stakes).

---

## 2. The Buyer & positioning (ADR-0001, ADR-0009)

**Buyer:** the agency or freelance developer who just sold a client an AI feature and must ship it on a deadline, defensibly. Not the indie hacker (they are the free-content funnel), not the end user.

**Headline:** "Production RAG + AI-support starter kit: permission-aware retrieval, isolation and answer-quality proven by evals, no framework lock-in." "No LangChain" is a supporting bullet, not the identity.

**Moat (the thing no competitor and no course has):** rigor that is *demonstrated, not asserted*. Two proofs:
1. **Access is correct** — the leak/correctness eval shows zero gold-chunk leakage to unauthorized viewers (built: pre-filter 1.000).
2. **Answers are trustworthy** — retrieval quality and (new) escalation correctness are scored in CI against golden sets.

**Honesty as a feature:** the landing page carries an explicit capability matrix (formats, providers, auth modes, what's out of scope). The Buyer is quoting client projects; telling them exactly what they can promise *is* product value.

**Go-to-market:** portfolio-first, built in public. The development process is the distribution engine; the open-source core is top-of-funnel; sales are the trailing indicator. Pricing deferred until a waitlist exists (candidate $149–$249).

---

## 3. Product scope

### 3.1 Two faces, one engine

The kit ships one retrieval/eval engine with two demoable configurations:

- **Knowledge-assistant face** — internal "chat with your docs," the existing reference app.
- **Support-platform face** — customer-facing deflection with escalate-to-human, an embeddable widget, and workspace/team structure (the "Echo category," built on this engine rather than on Convex).

The Buyer picks a face per client; the engine, evals, and isolation are shared.

### 3.2 In scope (v1)

Permission-aware hybrid RAG · Workspace/org isolation · eval harness (retrieval + escalation) · self-hosted observability · OpenAI-compatible + Azure model surface · digital-native ingestion behind a swappable boundary · reference web app · embeddable support widget · escalation/auto-resolve capability · per-developer license + GitHub delivery · landing page + open-core split.

### 3.3 Out of scope (v1) — documented, not fudged

VAPI/voice (future paid add-on) · Convex/any LLM framework · Anthropic/Bedrock-native APIs · OCR/scanned-doc parsing · billing/payments as core (optional flag only) · nested groups · per-chunk override UI · audit-log UI · fine-tuning · knowledge graphs · multi-step SQL plans · MFA/SCIM/magic links.

---

## 4. Architecture (reconciled)

**Stack (as built, retained):** React/Vite/Tailwind/shadcn frontend · Python/FastAPI backend · Supabase (Postgres + pgvector + Auth + Storage + Realtime) · raw OpenAI SDK + Pydantic, no LLM frameworks · Vercel + Railway/Fly + Supabase deploy.

**Trust boundary:** the database. Retrieval (`match_chunks`, `SECURITY INVOKER`) resolves the viewer's principal set server-side from `auth.uid()`; the backend never passes principal IDs. Workspace isolation (new) layers above the existing owner-OR-ACL predicate.

**Three boundaries kept narrow and swappable:**
- *Identity Boundary* — verified identity in → tenant/workspace context out (default: Supabase Auth).
- *Ingestion Boundary* — files in → structured text + metadata out (default: docling for digital-native formats).
- *Model surface* — OpenAI-compatible `base_url`/model/auth, config-driven.

---

## 5. Functional requirements

### 5.1 Retrieval & permissions core

| ID | Requirement | Status | Source |
|---|---|---|---|
| R1 | Hybrid retrieval: vector (pgvector HNSW) + keyword (Postgres FTS) fused via RRF | `BUILT` | FR-11 |
| R2 | Owner-OR-ACL predicate inside `match_chunks`, `SECURITY INVOKER`, principal set resolved from `auth.uid()` | `BUILT` | FR-37 |
| R3 | Per-chunk granting-principal attribution (owner > direct > group) surfaced as UI badges | `BUILT` | FR-39 |
| R4 | Optional reranker layer (cohere/voyage/llm/none) | `BUILT` (optional module) | FR-12, A6 |
| R5 | Re-ingestion preserves grants via snapshot-and-replay | `BUILT` | FR-41 |
| R6 | **Workspace entity** scoping Principals and Documents; minimal membership admin | `TODO` | A1, GAP-B1 |
| R7 | **Workspace-boundary RLS** layered above owner-OR-ACL | `TODO` | A1, GAP-B2 |

### 5.2 Ingestion (ADR-0005)

| ID | Requirement | Status | Source |
|---|---|---|---|
| I1 | Drag-and-drop ingestion: txt/md/pdf/docx/html via docling; chunk → embed → index; Realtime status | `BUILT` | FR-6,7,10 |
| I2 | Heading-aware recursive chunking, token-sized + overlap, citation metadata; configurable params, single strategy | `BUILT` | ADR-0005 |
| I3 | Content-hash dedup + incremental updates | `BUILT` | FR-9 |
| I4 | LLM structured-output document metadata extraction | `BUILT` | FR-8 |
| I5 | **Verify docling sits behind one narrow Ingestion Boundary**; refactor if threaded through pipeline | `PARTIAL` | GAP-F2 |
| I6 | **Commercial-parser adapter example** (Unstructured/LlamaParse) for OCR/complex layouts | `TODO` | ADR-0005, GAP-F2 |

### 5.3 Identity & isolation (A2, supersedes ADR-0006)

| ID | Requirement | Status | Source |
|---|---|---|---|
| AU1 | Supabase Auth: email/password + OAuth (Google, GitHub) | `BUILT` | FR-1 |
| AU2 | RLS scoped to `auth.uid()` on all user-data tables | `BUILT` | FR-2 |
| AU3 | Identity Boundary documented as swappable (for clients refusing the default IdP) | `PARTIAL` | ADR-0006 intent |
| AU4 | **API-layer auth attack tests**: forged / missing / expired / cross-workspace JWTs against every endpoint | `TODO` | GAP-B4 |

### 5.4 Model surface (ADR-0004)

| ID | Requirement | Status | Source |
|---|---|---|---|
| M1 | Raw OpenAI SDK; Responses + Chat Completions behind a common streaming interface; manual tool loop capped at 5 iters | `BUILT` | FR-5 |
| M2 | **Config-driven `base_url`/model/auth; test + document against Azure OpenAI** | `TODO` (claimed, unverified) | ADR-0004, GAP-C |
| M3 | Embedding endpoint config-driven + documented "change model → re-index" warning | `TODO` | ADR-0004, GAP-C2 |

### 5.5 Support-platform layer (new — the Echo category, on this engine)

| ID | Requirement | Status | Source |
|---|---|---|---|
| S1 | Escalation / auto-resolve decision as a first-class capability | `TODO` (design settled) | **ADR-0003** (ruled) |
| S2 | Escalation signal: **retrieval-grounded (weak retrieval → escalate) + faithfulness-grounded (draft answer unsupported by context → escalate)**, run as a retrieval-first cascade | `TODO` (design settled) | **ADR-0003** (ruled), §11 |
| S3 | Embeddable chat widget (`<script>` drop-in) honoring per-workspace ACLs | `TODO` (security model settled) | Echo-take; CONTEXT.md *Support widget surface* |
| S4 | Conversation state model (`active/escalated/resolved`, escalation latches) + human-handoff surface | `TODO` (design settled) | **ADR-0004** (ruled) |
| S5 | Teams within a Workspace (folds into R6) | `TODO` | A1, Echo-take |
| S6 | BYO model-key per workspace (multi-tenant SaaS clients) | `TODO` (low priority) | Echo-take |
| S7 | Billing (Stripe/Clerk) | `TODO` (optional flag, behind feature switch) | A6, Echo-take |

### 5.6 Eval harness (ADR-0003, A5)

| ID | Requirement | Status | Source |
|---|---|---|---|
| E1 | Retrieval eval: 50q golden set, recall@{1,3,5,10}, MRR, nDCG@5, vector/keyword/hybrid, deterministic | `BUILT` | FR-29,30 |
| E2 | Generation eval: cross-family LLM judge (Claude judging GPT) faithfulness/helpfulness, opt-in | `BUILT` | FR-33 |
| E3 | RAGAS weekly (faithfulness, answer relevancy, context precision/recall) | `BUILT` | ragas PRD |
| E4 | Permission correctness eval: 50×3 viewer-parameterized — security (0 leak) / recall-tradeoff / non-regression | `BUILT` | FR-42 |
| E5 | Scale benchmark: 10k Wikipedia chunks, ef_search sweep, nightly, recall-floor regression alarm | `BUILT` | FR-43 |
| E6 | **Workspace-boundary cases added to correctness eval** (no grant crosses a Workspace) | `TODO` | A1, GAP-B3 |
| E7 | **Escalation Golden Set + runner**: labeled answer-vs-escalate set (P1a/P1b/P2/P3 populations); deflection rate @ false-resolve ceiling; false-escalate rate; P1b non-disclosure assertion | `TODO` (design settled) | **ADR-0003** (ruled) |
| E8 | **Configurable CI gate** `off \| comment \| fail` with per-metric thresholds (default comment; flake rationale documented) | `TODO` | A5 |
| E9 | **Buyer-facing Golden Dataset format + authoring guide** (genericize the project-specific set into methodology) | `TODO` | GAP-E1 |
| E10 | Generic corpus-seeder template; demo corpora (Acme/CRM/Wikipedia) preserved as worked examples | `TODO` | GAP-E3 |

### 5.7 Observability (ADR-0003, A3)

| ID | Requirement | Status | Source |
|---|---|---|---|
| O1 | **Postgres trace store**, **operator-privileged / outside `match_chunks`** (service-role, never member-reachable; `workspace_id` stamped, per-ws RLS deferred to S6). **Hybrid content**: references+scores+gate-decisions+tokens+latency for all turns; verbatim context only for escalated+sampled. **Two-window retention** via time-partitioning + a targeted erasure path (erasure = de-identify, keep analytics signals) | `TODO` (design settled) | GAP-D1; CONTEXT.md *Observability* |
| O2 | Trace + eval-run dashboard as a **separate standalone ops service** (max isolation; optional/opt-in deploy unit), **infra-level operator auth** (IAP/VPN/reverse-proxy, localhost-default, fail-closed). Eval history via a thin optional `eval_runs` table (JSON stays source of truth) | `TODO` (design settled) | GAP-D2; CONTEXT.md *Observability* |
| O3 | **One tracing facade → two sinks**: structured Postgres (primary, queryable) + OTel spans (optional → OTLP/LangSmith/Langfuse). LangSmith **demoted to one exporter** | `PARTIAL` (LangSmith-specific today, not yet OTel/facade) | GAP-D3; CONTEXT.md *Observability* |
| O4 | Error capture folded into the facade (error-status span + trace event on both sinks); **no Sentry** | `TODO` (design settled) | A3, Echo-take; CONTEXT.md *Observability* |

### 5.8 Frontend & contract (A4, ADR-0007)

| ID | Requirement | Status | Source |
|---|---|---|---|
| F1 | React/Vite reference app: streaming chat w/ citations, doc upload, share dialog, nested tool-call tree | `BUILT` | FR-3,16,18 |
| F2 | **OpenAPI contract polish + generated typed TS client** (the real deliverable under the reference UI) | `TODO` | GAP-F1 |
| F3 | **Buyer-facing capability matrix** (honest-marketing form of the non-goals list) | `TODO` | GAP-F3 |

### 5.9 Optional modules (A6 — keep, mark optional, document deletion)

| ID | Requirement | Status | Source |
|---|---|---|---|
| X1 | Text-to-SQL via hand-authored semantic layer (`plan_query` + `sql_search`, allowlisted read-only) | `BUILT` (optional) | FR-22–26 |
| X2 | Web-search fallback tool | `BUILT` (optional) | FR-15 |
| X3 | Sub-agents (`spawn_document_agent`) | `BUILT` (optional) | FR-17 |
| X4 | Each optional module gets a documented "rip-this-out" path; quickstart runs without them | `TODO` | A6 |

---

## 6. Productization (GAP group A — biggest bucket, ~0% built)

| ID | Requirement | Status | Source |
|---|---|---|---|
| P1 | Template-ization: genericized seeders, project rename/rebrand scaffolding, demo corpora → optional examples, 30-min zero-to-deployed quickstart | `TODO` | GAP-A1 |
| P2 | License + delivery: per-developer license text, private-repo onboarding, CHANGELOG discipline + documented-diff updates, support-boundary terms | `TODO` | ADR-0008, GAP-A2 |
| P3 | Landing page + waitlist: capability matrix, **leak-eval demo video** (record the 1.000 no-leak table), pricing decision | `TODO` | ADR-0009, GAP-A3 |
| P4 | Open-core split: open-source single-user core (chat + ingestion + hybrid retrieval); paid = permissions, evals, workspace, escalation | `TODO` | ADR-0009, GAP-A4 |
| P5 | Buyer-facing threat-model document (the security-review artifact the agency hands the client) | `TODO` | GAP-A5 |
| P6 | Positioning rewrite: README/marketing speaks Buyer language, not researcher; leads with the moat, not LangSmith | `TODO` | ADR-0001, GAP-A6 |

---

## 7. Commercial structure (ADR-0008)

Per-developer license, unlimited end products, no resale as a competing template · delivery via private GitHub repo · updates as documented diffs in a disciplined CHANGELOG (no merge automation, no plugin architecture) · 12 months of updates, optional renewal · support = community Discord + GitHub issues; debugging customized client deployments is consulting · **launch price deferred** until the waitlist exists.

---

## 8. Build-in-public plan (ADR-0009)

The dev process is the distribution engine (30–40% of total effort, not garnish):
1. **Open core first** — strip to single-user chat + ingestion + hybrid retrieval; publish.
2. **Content cadence** — one flagship post per build phase. The best material already exists in `docs/`: the permission-aware-RAG writeup (post-filter recall math + HNSW gotcha) and the eval writeup (Δ-0.510 regression caught in CI). These publish *now*.
3. **Interviews in parallel** — 10 buyer conversations via the audience. Probe: "tell me about the last AI feature you shipped for a client — what ate your time?" If isolation/evals/escalation don't surface unprompted, revisit the hero before launch.
4. **Waitlist before paywall.**
5. **Success metrics in order:** audience → credibility → revenue.

---

## 9. Build sequence

1. **Rulings** — confirm/overrule Section 1 assumptions + answer the Section 11 open question. (One conversation; finalizes the `TODO (open)` items.) **[Done 2026-06-16: §11 ruled → ADR-0003; A1/A2 ruled → ADR-0002; S4 designed → ADR-0004; O1–O4 designed → CONTEXT.md. A3–A6 stand as assumed.]**
2. **Workspace layer** — R6, R7, E6, AU4. Completes the org-isolation half of the hero.
3. **Escalation capability** — S1, S2, E7 (once §11 ruled). Completes the support face + extends the moat.
4. **Self-hosted observability** — O1–O4. Removes the LangSmith contradiction.
5. **Eval genericization + gate** — E8, E9, E10. Turns project evals into Buyer methodology.
6. **Model surface + contract + boundaries** — M2, M3, I5, I6, F2, F3, AU3. Closes claim-vs-verified gaps.
7. **Support surface** — S3, S4, S5 (widget, conversation state, teams).
8. **Productization** — P1–P6, X4. Last, because everything above changes the landing page — but P4 (open-core split) and P6 (positioning) and the flagship posts can start in parallel from day one.

---

## 10. Risks (the three ways this fails)

1. **The distribution engine never reaches escape velocity.** Build-in-public only works if the building is actually published, consistently — a second full-time skill. *Tripwire:* if after the first two flagship posts (permission-RAG + leak suite — your best material) the waitlist is under ~50, fix distribution before more build, not after launch. The failure mode is writing code instead of posts because code is more comfortable.

2. **The hero feature is right but the *use case* is wrong — chosen on zero interviews.** You're betting agency clients want eval-proven isolation and deflection. The plausible alternative: they want speed-to-demo and the parsing-hell solved, and your hardest 40% is invisible to them. *Tripwire:* the 10 interviews are still mandatory, in parallel; if fewer than 3 of 10 raise isolation/evals/escalation unprompted, demote the moat in *marketing* (keep it in product) and lead with the support-platform demo.

3. **The maintenance tail kills a security-sensitive solo asset.** The whole pitch is "proven correct"; one leak bug found by a buyer's client *inverts* the thesis — the public artifact now documents the failure. Slower deaths: Supabase/pgvector/FastAPI drift, a CHANGELOG that goes quiet, support eating the time the content engine needs, and now an *escalation* path where a False-Resolve sends a customer a confidently-wrong answer in production. *Tripwire:* before launch, commit an honest maintenance budget (hours/month for 18 months) and a sunset story (price drop + open-source schedule). If you can't, ship the open core with brilliant writeups and skip the paywall — 80% of the portfolio value, 10% of the tail risk.

---

## 11. Open question - RESOLVED (2026-06-16) by the ADR-0003 ruling

> **Ruling:** signal source = **3 + 4 as a retrieval-first cascade** — evaluate the cheap retrieval gate first (escalate with no draft when retrieval is weak), and draft + run the faithfulness gate only on strong retrieval. The OR short-circuit is the cost win. Accepted cost: a strong-retrieval-but-unfaithful query pays for a draft it won't send. This ruling is captured in this section (§11) and the §12 crosswalk; the on-disk ADR file for it is not yet written. The options and rationale below are retained as historical context.

**Escalation signal source.** ADR-0010 settled *that* escalation is eval-backed; it did not settle *what drives the decision*. Options:

- (2) Standalone classifier call — faster to build, weakest differentiation, cloneable.
- (3) Retrieval-grounded — escalate when retrieval is weak; reuses your retrieval machinery.
- (4) Faithfulness-grounded — draft an answer, escalate when it isn't supported by retrieved context; reuses your existing RAGAS faithfulness signal.

**Recommended default baked into this PRD: 3 + 4 combined** — escalate when retrieval is weak *or* the draft answer isn't faithful. This makes escalation an emergent property of the moat you already ship (same recall + faithfulness signals, same eval infra scores both), which Echo structurally cannot copy. **Accepted cost:** escalated conversations pay for a draft answer they won't send (extra model call + latency). **Ruled: 3+4 retrieval-first cascade accepted (ADR-0003); S1, S2, E7 unlocked.**

---

## 12. Superseded / amended ADRs

- ADR-0002 amended by A1 (hero = sharing + workspace isolation).
- ADR-0006 superseded by A2 (Supabase Auth, not Clerk).
- ADR-0007 superseded by A4 (Vite, not Next.js).
- ADR-0003 reaffirmed; refined by A5 (configurable gate) and A3 (LangSmith optional).
- ADR-0010 extended by Section 11 - **signal source now RULED → ADR-0003** (ruling; on-disk ADR file not yet written).
- All others (0001, 0004, 0005, 0008, 0009) carried forward unchanged.

### ADR numbering crosswalk (internal vs. Phase-2 `docs/adr/`)

The ADR numbers referenced *above and throughout this PRD* (ADR-0001…0010) are the **internal** numbering inherited from the original design docs. The **Phase-2** `docs/adr/` numbering is a **separate, later** sequence produced during Phase-2 grilling (0001–0002 written to disk; 0003–0004 ruled, files not yet written). They do **not** correspond by number — e.g. PRD-internal "ADR-0002" = the sharing model, but on-disk `0002` = workspace isolation. Map by topic:

| Phase-2 ADR (`docs/adr/`) | Topic | Resolves / supersedes (PRD-internal) |
|---|---|---|
| 0001 | RAGAS as a parallel eval signal | E3 / ragas PRD |
| 0002 | Workspace tenant isolation above owner-OR-ACL | A1 (amends internal ADR-0002), A2 (supersedes internal ADR-0006); R6/R7/E6/AU4 |
| 0003 *(ruled; file not yet written)* | Escalation signal source + deterministic deflection pipeline | §11 ruling (extends internal ADR-0010); S1/S2/E7 |
| 0004 *(ruled; file not yet written)* | Support-conversation state + human-handoff surface | S4 |
| *(none yet)* | Observability (O1–O4) — captured in `CONTEXT.md` *Observability & tracing*, ADR deferred | O1–O4 |
