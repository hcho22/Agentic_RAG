# ADR 0007: Ingestion parser boundary (docling behind a swappable seam)

- **Status:** Accepted
- **Date:** 2026-06-19

## Context

Document ingestion converts an uploaded file (PDF / DOCX / HTML / MD / TXT)
into normalized text that the chunker can split. Today that conversion is done
by `docling`, which lives only in `backend/parsing.py`: the single entry
`parse_document(raw, filename, content_type) -> str` (`backend/parsing.py:439`)
returns normalized markdown, `.txt` bypasses docling and is returned verbatim,
and the `pypdfium2` PDF fallback is contained inside the same module. Nothing
docling-typed is consumed downstream — `backend/chunking.py`'s
`chunk_text(text: str, ...)` (`backend/chunking.py:109`) operates on a plain
markdown string with zero docling awareness.

The Phase-2 PRD listed this boundary (I5) as **"PARTIAL — refactor if docling
types are threaded through the codebase."** That qualifier turned out to be
moot: docling was never threaded. So I5 is a *verify-and-formalize*, not a
refactor — there is no threading to undo. This ADR records (a) the verified
clean state, and (b) the decision to lift the implicit function seam into an
explicit, swappable extension point so a buyer can replace the parser — most
importantly to reach the OCR / complex-layout documents docling's default
text pipeline does not cover (PRD §3.3).

## Decision

The ingestion parser is a **single-module seam with a pinned markdown-string
output contract**, lifted into the same protocol + factory pattern the kit
already uses for rerankers and web search.

- **I5 is verified, not refactored.** docling is imported in exactly one
  module (`backend/parsing.py`); no other `backend/*.py` imports docling or
  names a docling-only type (`DocumentConverter` / `InputFormat` /
  `DocumentStream`), and the chunker consumes only `str`. The PRD "PARTIAL /
  refactor if threaded" qualifier is therefore moot. This is enforced by a
  regression test (`backend/test_parsing_seam.py`, US-036) that fails loudly if
  docling ever leaks past `parsing.py` — evidence, not assertion.

- **The output contract is a normalized markdown `str` (v1, pinned).** This is
  the load-bearing coupling between the boundary and the chunker: the chunker is
  markdown/heading-aware only, so a parser with native structured output (tables
  / layout / bounding boxes) **must flatten to markdown at the boundary**. The
  `str` contract is what makes any parser interchangeable without touching
  `chunking.py`.

- **Lift the implicit function seam into a `DocumentParser` protocol +
  `build_parser` factory** (US-037–039), mirroring `Reranker`
  (`backend/reranking.py`) and `WebSearchProvider` (`backend/web_search.py`):
  a one-method ABC (`parse(raw, filename, content_type) -> str`) and a
  `PARSER=docling|unstructured|llamaparse` env switch resolved at the call site.
  Default `PARSER=docling` preserves today's behavior exactly. Commercial
  adapters fail at **build time** on a missing API key (matching
  `build_reranker` / `build_web_search_provider`), and an unimplemented value
  fails loudly — **never a silent fallback to docling**.

- **The `pypdfium2` PDF fallback stays inside the docling adapter.** It is a
  docling-path concern (docling's torch pipeline failing on some base images),
  not a boundary concern, so it is not promoted to the protocol.

- **Ship exactly one real commercial adapter: LlamaParse** (US-040–041) for the
  OCR / scanned / complex-layout gap docling cannot cover, requesting markdown
  result type and flattening any structured output at the boundary. It is
  smoke-tested against the live API when `LLAMA_CLOUD_API_KEY` is present and
  skipped (not failed) when absent.

## Consequences

- Swapping the ingestion parser becomes a config switch (+ an API key for
  commercial adapters), not a code change. The chunker, ingest path, and the
  rest of the backend stay parser-agnostic.
- The verified seam is a standing invariant: `backend/test_parsing_seam.py`
  keeps docling confined to `parsing.py` and keeps the chunker on `str`, so a
  future regression (e.g. importing a docling type into `main.py`) breaks CI
  rather than silently re-coupling the codebase.
- A "write your own adapter" guide (US-043,
  [`docs/ingestion-parser-adapters.md`](../ingestion-parser-adapters.md)) makes
  adding a parser a supported path — the markdown-string contract, the edits
  (subclass + `PARSER` validation + `build_parser` registration), `PARSER`
  selection, and the US-041 smoke test as the round-trip proof template — with
  **Unstructured.io** named as the canonical buyer-written example behind the
  same protocol and LlamaParse as the worked reference.
- The accepted gaps are disclosed as F3 capability-matrix rows citing this ADR
  (US-044, the matrix lives in
  [`docs/ingestion-parser-adapters.md`](../ingestion-parser-adapters.md) §
  "F3 capability matrix"): OCR/scanned is out-of-scope for the *default* parser
  (LlamaParse only); the boundary output is a markdown string (table/layout
  fidelity beyond markdown is a documented future widening); `PARSER=unstructured`
  is a buyer-written adapter slot, not a shipped impl.

## Alternatives considered and rejected

- **Ship Unstructured.io as the example commercial adapter.** Rejected as the
  *shipped* one in favor of LlamaParse — chosen for the simplest API and the
  strongest OCR / complex-layout coverage, which is precisely the gap docling
  cannot cover. Unstructured.io remains a fully valid choice and is the named
  canonical buyer-written ("bring your own adapter") example behind the same
  protocol.
- **Widen the boundary output type now to carry structure** (tables / bounding
  boxes / layout) for a future layout-aware chunker. Rejected for v1 — it is a
  larger change that also touches `chunking.py`. The markdown-string contract is
  a deliberate v1 pin with a known upgrade path (US-045), not an oversight; an
  adapter author must not return non-`str` today.
- **Promote the `pypdfium2` fallback to the boundary.** Rejected — it is a
  docling-implementation detail; promoting it would leak a docling concern into
  the parser-agnostic boundary.
- **Leave the seam implicit (a bare function).** Rejected — an implicit seam is
  not a discoverable extension point. The explicit ABC + factory makes "swap the
  parser" consistent with the two boundaries (`Reranker` / `WebSearchProvider`)
  a reader already knows.
