# ADR 0011: Product name — Purvia

- **Status:** Accepted
- **Date:** 2026-07-14
- **On:** ADR-0002 (workspace tenant isolation), ADR-0003 (deterministic escalate-vs-answer control flow)

## Context

The product has never had a real brand. "Agentic RAG" is only a working/repo
name that leaked into a handful of vendor-facing display strings (browser
title, app header, login tagline, API title, README/CONTEXT headings). Before
going to market we need the product's first real name.

Two things constrain the choice. First, the product is a **platform** with two
surfaces under one roof — the owner-only knowledge-assistant (`threads`/
`messages`) and the white-labeled support widget (`conversations`/support bot) —
so the name must describe the whole, not one module. Second, the white-labeled
widget renders the *buyer's* brand, never the vendor's, so the vendor name must
never leak across that boundary; it names the platform a buyer integrates, not
anything the buyer's end-users see.

The product's actual differentiator is **permissions-aware retrieval**: per-
document sharing is a first-class part of the retrieval predicate (ADR-0002),
not a post-hoc filter. The name should point at *that*.

## Decision

The product is named **Purvia**.

- **"Purvia" is coined from *purview*** — "the scope of what one is authorized
  to see and know." This is the "need-to-know / purview" semantic direction: the
  name points directly at the permissions-aware differentiator (who is
  authorized to see what) rather than at any one surface or wedge.

- **"Deflio" was considered and rejected.** It was coined from *deflection* and
  named only the support-deflection wedge — one white-labeled module (the widget
  bot auto-resolving tickets). Naming the platform after a single module is
  mis-scoped: it undersells the owner-only knowledge-assistant surface and boxes
  the brand into support tooling when the product is a permissions-aware
  knowledge platform. The purview framing scopes to the whole product; the
  deflection framing scopes to one feature. Purview won.

- **`.ai`-only is accepted.** `purvia.com` is parked (registered 2012) and the
  `.com` space for coined fusion-field names is saturated; the active `.ai`
  land-grab makes `purvia.ai` the pragmatic launch domain. Registering `.com`
  later, or acquiring the parked one, is a follow-up, not a blocker.

This is a **display-only rename plus this ADR**. Only vendor-facing display
strings change; no behavior, logic, RLS, contract, or deploy identifier moves.

## Consequences

- **The technical namespace deliberately stays `agentic-rag`/`ar`.** All of it is
  brand-neutral and load-bearing for live customer tokens, buyer embed contracts,
  and deploy/observability continuity: the `ar-support:*` localStorage prefixes,
  `window.SupportWidget`/`SupportWidgetSettings` and the `supportwidget:unread`
  event, the `ar-support-widget@1` postMessage channel, `frontend/package.json`
  name `agentic-rag-frontend`, logger `agentic_rag.backend`, Supabase
  `project_id = "agentic-rag"`, Fly app `agentic-rag-backend`, LangSmith project
  `agentic-rag`, and the `wk_pk_` widget-key prefix. Renaming any of these is a
  separate, later, higher-risk effort — not part of this decision.

- **The white-labeled widget is unaffected.** It renders the buyer's brand (e.g.
  a demo `data-title="FitSnack Support"`), never "Purvia." The white-label
  boundary is exactly what keeps the vendor rename from bleeding into buyer
  surfaces; that isolation is a pinned check of the rebrand (US-006).

- **A paid USPTO Class 9 + 42 trademark clearance is still pending.** Our search
  was directional only. A consumer "Purvia" AI face-swap app exists in a
  different class; it should still be formally cleared before branding spend.

## Alternatives considered and rejected

- **"Deflio."** Rejected — mis-scoped to the support-deflection wedge (a single
  white-labeled module) when the product is a platform; see Decision above.

- **Keep "Agentic RAG."** Rejected — it is a working/category description, not a
  brand: generic, un-ownable, and it says nothing about the permissions-aware
  differentiator. Fine as an internal/technical slug, not as a market name.

- **Renaming the technical namespace too.** Rejected for now — it risks live
  customer tokens, buyer embed contracts, and deploy/observability continuity for
  no branding gain (the slugs are never shown to prospects). Deferred as a
  separate higher-risk effort.
