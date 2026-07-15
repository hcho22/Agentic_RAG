# PRD: Rebrand to "Purvia"

## Introduction

The product currently has no real brand. "Agentic RAG" is only a working/repo name that leaks into a handful of vendor-facing display strings (browser title, app header, login tagline, API title, README/CONTEXT headings). Before going to market, we are establishing the product's first real brand: **Purvia**.

"Purvia" is coined from *purview* - "the scope of what one is authorized to see and know." This names the product's actual differentiator: a **permissions-aware knowledge platform** where per-document sharing is a first-class part of the retrieval predicate. (The name was chosen over the earlier candidate "Deflio," which was retired because it named only the support-deflection wedge - one white-labeled module - rather than the platform itself. The full decision trail lives in the plan file and will be recorded in ADR-0011.)

This is a deliberately **tight, display-only rename plus a naming ADR**. The entire technical namespace (`agentic-rag`, `ar-support:*`, `window.SupportWidget`, deploy/observability slugs) stays untouched because it is brand-neutral and load-bearing for live customer tokens, buyer embed contracts, and deployment continuity. The white-labeled support widget is unaffected - it renders the *buyer's* brand, never the vendor's.

## Goals

- Replace every vendor-facing "Agentic RAG" display string with "Purvia" (6 known locations).
- Reposition the README tagline to describe Purvia as a permissions-aware knowledge platform.
- Record the product name as a glossary term in `CONTEXT.md`.
- Capture the naming decision (and the rejection of "Deflio") in a committed ADR.
- Introduce **zero** behavior, logic, RLS, or contract changes - this is a pure branding/display change.
- Prove the white-label boundary still holds: the embedded widget continues to show the buyer's brand, not "Purvia."

## User Stories

### US-001: Rename vendor-facing frontend display strings

**Description:** As a prospective customer visiting the app, I want to see the product's real name "Purvia" so the brand is consistent and market-ready.

**Status:** ✅ Done (2026-07-15, commit `2e71b4b`, PR #88) - three one-line display-string edits; typecheck+build green; browser-verified (tab title, header, login tagline).

**Acceptance Criteria:**

- [x] `frontend/index.html:6` - `<title>Agentic RAG</title>` → `<title>Purvia</title>`
- [x] `frontend/src/components/AppHeader.tsx:42` - header `<h1>` text `Agentic RAG` → `Purvia`
- [x] `frontend/src/pages/LoginPage.tsx:57` - "Access your Agentic RAG workspace." → "Access your Purvia workspace."
- [x] No other logic or styling changed in these files
- [x] `npm run typecheck && npm run build` passes
- [x] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** Frontend running locally; log out so the login page is reachable.
- **Steps:**
  1. Open the app in a browser and read the browser tab title.
  2. Log in and read the top-of-app header text.
  3. Log out and read the login page tagline.
- **Expected Result:** Tab title reads "Purvia"; header reads "Purvia"; login tagline reads "Access your Purvia workspace." No "Agentic RAG" remains on any of these surfaces.
- **Failure Indicator:** Any surface still shows "Agentic RAG", the header layout breaks, or the build/typecheck fails.

### US-002: Rename backend API title

**Description:** As a developer or integrator viewing the API docs, I want the service identified as "Purvia" for brand consistency.

**Status:** ✅ Done (2026-07-15, commit `5c13875`, PR #89) - two one-line display-string edits (FastAPI title + module docstring); import-verified (`app.title` reads "Purvia backend"); logger name intentionally unchanged.

**Acceptance Criteria:**

- [x] `backend/main.py:813` - `FastAPI(title="Agentic RAG backend")` → `FastAPI(title="Purvia backend")`
- [x] `backend/main.py:1` module docstring "FastAPI backend for the Agentic RAG app." → "...for the Purvia app."
- [x] No route, handler, logic, or logger-name change (logger stays `agentic_rag.backend` - see Non-Goals)
- [x] Backend imports/starts without error

**Validation Test:**

- **Setup:** Backend runnable locally (support features may be inert without secrets - that is fine).
- **Steps:**
  1. Start the backend.
  2. Open `/docs` (FastAPI Swagger UI) or fetch the OpenAPI schema.
- **Expected Result:** The API title reads "Purvia backend." The service starts cleanly with no import errors.
- **Failure Indicator:** Title still says "Agentic RAG backend," or the app fails to start.

### US-003: Rebrand README (heading + tagline + docs index)

**Description:** As anyone landing on the repo's front page, I want it to present "Purvia" as a permissions-aware knowledge platform so the positioning is clear.

**Status:** ✅ Done (2026-07-15, commit `8f039d1`, PR #90) - README-only: H1, tagline reposition (factual, existing retrieval-predicate framing preserved), ADR-0011 docs-index row. The linked ADR file itself lands with US-005; the link is intentionally ahead of it per the story split.

**Acceptance Criteria:**

- [x] `README.md:1` - `# Agentic RAG` → `# Purvia`
- [x] Tagline rewritten to position Purvia as a permissions-aware knowledge platform (per-document sharing as a first-class part of the retrieval predicate). Keep it factual; do not overclaim.
- [x] The `## Documentation` index gains an entry for the new ADR (`docs/adr/0011-product-name.md`).
- [x] Internal/technical references that are intentionally retained (e.g. instructions that reference the `agentic-rag` slug, Fly app name) are left unchanged and NOT mass-replaced.

**Validation Test:**

- **Setup:** None.
- **Steps:**
  1. Open `README.md` and read the H1 and the tagline immediately below it.
  2. Scan the `## Documentation` section for the ADR-0011 link.
- **Expected Result:** H1 reads "# Purvia"; the tagline describes a permissions-aware knowledge platform; the ADR-0011 entry is present and correctly linked.
- **Failure Indicator:** H1 still says "Agentic RAG," the tagline still reads as a generic RAG description, or the ADR link is missing/broken.

### US-004: Add "Purvia" glossary term and rename CONTEXT.md heading

**Description:** As a contributor, I want the product name defined in the project's ubiquitous-language glossary so the term is canonical and unambiguous.

**Acceptance Criteria:**

- [ ] `CONTEXT.md:1` - `# Agentic RAG — Context glossary` → `# Purvia — Context glossary`
- [ ] A glossary entry defines **Purvia** = the product/platform (a permissions-aware knowledge platform), and notes its two surfaces both live under it: the owner-only knowledge-assistant (`threads`/`messages`) and the white-labeled support widget (`conversations`/support bot).
- [ ] The entry follows the existing bold-term definition style used elsewhere in `CONTEXT.md`.
- [ ] No existing glossary terms are altered.

**Validation Test:**

- **Setup:** None.
- **Steps:**
  1. Open `CONTEXT.md`; read the H1.
  2. Locate the new "Purvia" definition.
- **Expected Result:** H1 reads "# Purvia — Context glossary"; a clear "Purvia" term defines the platform and names its two surfaces.
- **Failure Indicator:** Heading unchanged, term missing, or an unrelated term was edited.

### US-005: Author ADR-0011 (product name)

**Description:** As a future maintainer, I want the naming decision recorded so no one re-litigates it or wonders why the product is "Purvia" and not "Deflio."

**Acceptance Criteria:**

- [ ] New file `docs/adr/0011-product-name.md`, matching the existing ADR format: `# ADR 0011: <title>`, then `- **Status:** Accepted`, `- **Date:** 2026-07-14`, an optional `- **On:**` dependency bullet, then `## Context` and further sections.
- [ ] Content records: the product was effectively unnamed ("Agentic RAG" = working title); "Deflio" was considered and **rejected** as mis-scoped to the support-deflection wedge (a single white-labeled module) when the product is a platform; the semantic direction chosen was the "need-to-know / purview" fusion; "Purvia" was selected; `.ai`-only was accepted given `.com` saturation.
- [ ] Consequences note: the technical namespace deliberately stays `agentic-rag`/`ar`; the white-labeled widget is unaffected; a paid USPTO Class 9/42 trademark clearance is still pending.
- [ ] Referenced from the README `## Documentation` index (see US-003).

**Validation Test:**

- **Setup:** None.
- **Steps:**
  1. Open `docs/adr/0011-product-name.md`.
  2. Compare its header/metadata block against an existing ADR (e.g. `docs/adr/0010-deterministic-alpha-fusion.md`).
- **Expected Result:** The ADR exists, follows the house format, and clearly explains the Purvia decision and the Deflio rejection with the trade-offs.
- **Failure Indicator:** File missing, format diverges from existing ADRs, or the rationale/trade-off is absent.

### US-006: End-to-end rebrand verification (regression + white-label isolation)

**Description:** As the team, we need to confirm the rebrand is complete on vendor surfaces AND that it did not bleed into the white-labeled widget or change any behavior.

**Acceptance Criteria:**

- [ ] `cd frontend && npm run typecheck && npm run build` passes.
- [ ] A backend unit-layer story test runs green (e.g. `python -m backend.test_supabase_jwt`) - confirming no logic moved.
- [ ] `grep -rn "Agentic RAG" frontend/ backend/ README.md CONTEXT.md` returns only intentionally-retained internal/technical references (no stray vendor-facing display strings).
- [ ] The embedded support widget still renders the buyer's brand (e.g. demo `data-title="FitSnack Support"`), NOT "Purvia."
- [ ] Verify in browser using dev-browser skill.

**Validation Test:**

- **Setup:** Frontend + backend running; open `frontend/widget-host-example.html` (the demo host page with a buyer-supplied `data-title="FitSnack Support"`).
- **Steps:**
  1. Run the typecheck/build and the chosen backend unit test.
  2. Run the grep and review each remaining hit.
  3. Load the main app: confirm tab/header/login all say "Purvia."
  4. Load the widget host example: open the widget and read its title/greeting.
- **Expected Result:** Build and test pass; grep hits are all deliberate technical references; the main app reads "Purvia"; the widget shows "FitSnack Support" (buyer brand), proving white-label isolation is intact.
- **Failure Indicator:** Build/test fails, an unexpected vendor-facing "Agentic RAG" string remains, or the widget shows "Purvia" (white-label leak).

## Functional Requirements

- FR-1: The browser tab title, app header, and login tagline must display "Purvia" (US-001).
- FR-2: The FastAPI service title must be "Purvia backend" (US-002).
- FR-3: The README must present "Purvia" as its H1 with a permissions-aware-knowledge-platform tagline and must link ADR-0011 in its documentation index (US-003).
- FR-4: `CONTEXT.md` must be retitled to "Purvia — Context glossary" and must define "Purvia" as a glossary term naming the platform and its two surfaces (US-004).
- FR-5: A committed ADR `docs/adr/0011-product-name.md` must record the Purvia decision, the Deflio rejection, and the trade-offs, in the existing ADR format (US-005).
- FR-6: The change must introduce no behavior, logic, RLS, contract, or deploy-identifier changes; the white-labeled widget must remain buyer-branded (US-006).

## Non-Goals (Out of Scope)

- **Technical namespace rename** - all of the following stay exactly as `agentic-rag`/`ar` (brand-neutral, and renaming risks live customer tokens, buyer embed contracts, and deploy/observability continuity): `ar-support:*` localStorage prefixes, `window.SupportWidget`/`SupportWidgetSettings` and the `supportwidget:unread` event, the `ar-support-widget@1` postMessage channel, `frontend/package.json` name `agentic-rag-frontend`, logger `agentic_rag.backend`, Supabase `project_id = "agentic-rag"`, Fly app `agentic-rag-backend`, LangSmith project `agentic-rag`, the `wk_pk_` widget-key prefix.
- **Widget/bot generic defaults** - `DEFAULT_TITLE='Support'`, `DEFAULT_GREETING='Hi! How can we help?'` (`frontend/src/widget/WidgetApp.tsx:29-30`), and bot `display_name='Support Bot'` (`backend/support_bot.py:199`) are generic white-label placeholders, not vendor brand - leave as-is.
- **Demo/example names** - `Acme`, `FitSnack` are corpus/example fixtures, not the product brand - do not touch.
- **Repository / GitHub / clone-path rename** (`Agentic_RAG`) - out of scope for now.
- **Domain registration and trademark filing** - non-engineering pre-launch actions; tracked in Open Questions, not implemented here.
- **Logo / wordmark asset** - the header is currently pure text; no logo asset exists and none is created in this PRD.
- **Any product behavior, retrieval, permissions, or escalation change.**

## Design Considerations

- The app header (`AppHeader.tsx`) is text-only today - a plain string swap, no asset work.
- Keep the README tagline factual and aligned to the existing positioning language ("per-document sharing is a first-class part of the retrieval predicate"); this is a reposition, not a marketing rewrite.
- Reuse the existing ADR format verbatim from `docs/adr/0010-deterministic-alpha-fusion.md` (Status/Date/On bullets, `## Context` sections) so ADR-0011 is consistent.

## Technical Considerations

- Pure string/display + docs change; no migrations, no dependency changes, no API surface change.
- Frontend gate is `tsc` via `npm run typecheck && npm run build` (there is no ESLint config - `tsc` is the lint gate).
- Backend story tests each have a unit layer that runs without DB/secrets - use one as a fast regression signal.
- The white-label isolation check (widget shows buyer brand, not "Purvia") is the key guard that the rename stayed on vendor surfaces only.

## Success Metrics

- 100% of vendor-facing display strings read "Purvia"; zero stray vendor-facing "Agentic RAG" strings (grep-verified).
- Frontend typecheck/build and a backend unit test pass with no new failures.
- Zero behavior/contract regressions; the embedded widget still renders the buyer's brand.
- The naming decision is discoverable in one place (ADR-0011) and canonical in `CONTEXT.md`.

## Open Questions

- **Register `purvia.ai` now** - the `.ai` land-grab is active (multiple fusion-field `.ai` domains were registered within 2026 during our search). This should happen before any public branding.
- **Paid USPTO trademark clearance in Class 9 + 42** before branding spend - our search was directional; the consumer "Purvia" AI face-swap app is a different class but should be formally cleared.
- **Acquire `purvia.com`?** - currently parked (registered 2012); decide whether to pursue purchase or stay `.ai`-only.
- **Repo/GitHub rename timing** - if/when to rename `Agentic_RAG` and the technical slugs is a separate, later, higher-risk effort (deploy hostnames, Supabase ref, observability history) - not part of this PRD.
- **Wordmark/logo** - do we want a designed wordmark for the header, or is the text treatment sufficient for launch?
