# Write your own ingestion parser adapter

The kit ships two parsers — **docling** (default; text-based PDF / DOCX / HTML /
MD) and **LlamaParse** (the commercial OCR / complex-layout escape hatch). If
you need a different one — [Unstructured.io](https://unstructured.io), a
homegrown OCR pipeline, a vendor your company already pays for — adding it is a
**supported, three-edit path**, not reverse-engineering. This page is that path.

Design rationale lives in **ADR-0007** (the ingestion parser boundary) and
**CONTEXT.md → "Ingestion boundary (I5 / I6)"**; this page is the how-to.

## The one rule: return a normalized Markdown **string**

This is load-bearing, so it comes first. The chunker (`chunk_text`,
`backend/chunking.py`) is **Markdown / heading-aware only** — it splits on `#`
headings and blank-line paragraph breaks. The single thing coupling any parser
to the rest of the pipeline is therefore the boundary's output type:

> **`parse(...)` MUST return a normalized Markdown `str`.** A parser whose
> native output is *structured* — tables, bounding boxes, page layout, a JSON
> document object — **MUST flatten that to Markdown inside `parse`, at the
> boundary.** A non-`str` (or a vendor-typed object) must never leak downstream.

Concretely:

- Tables → Markdown pipe tables (`| col | col |`).
- Headings / sections → `#`, `##`, …
- Paragraphs → separated by blank lines.

Most commercial parsers can do this flattening *server-side* if you ask for a
markdown result type — that is exactly what the LlamaParse adapter does (it
requests the `markdown` result so LlamaParse flattens tables/layout before the
bytes ever reach us). Prefer that to flattening structure yourself.

> **Why `str` and not something richer?** Carrying structure (tables / bboxes /
> layout) across the boundary for a future *layout-aware* chunker is a
> deliberately deferred change that also rewrites `chunking.py` — it is **out of
> v1** (ADR-0007 → US-045). Do **not** try to return a non-`str` today; there is
> nothing downstream that consumes it, and it breaks the seam the whole boundary
> exists to protect. The `str` pin is a decision with a known upgrade path, not
> an oversight.

## The contract

Your adapter implements one abstract method on `DocumentParser`
(`backend/parsing.py`), the same one-method ABC pattern as `Reranker`
(`backend/reranking.py`) and `WebSearchProvider` (`backend/web_search.py`):

```python
class DocumentParser(ABC):
    name: str  # must equal the PARSER value that selects it

    @abstractmethod
    def parse(self, raw: bytes, filename: str, content_type: str | None = None) -> str:
        ...
```

- **Input** is the triple the ingest path already holds: `raw` bytes, the
  original `filename`, and an optional `content_type` (a MIME type; the browser
  sometimes sends none, so treat it as a hint, not a guarantee).
- **Output** is a normalized Markdown `str` (see the rule above).
- **Errors:** raise `UnsupportedFormatError` / `ValueError` (or let an HTTP
  error propagate for cloud adapters). The ingest endpoint catches these and
  writes them to `documents.error_message` (status `error`), so make the message
  human-readable. Never silently return empty/garbage on failure.

## Worked example: a minimal `EchoParser`

The smallest possible adapter — it decodes the bytes as UTF-8 and returns them
(only valid for already-Markdown / plain-text input; a real adapter does real
conversion). Use it to walk the three edits, then swap in your real logic.

### Step 1 — Subclass `DocumentParser` (in `backend/parsing.py`)

Set a class-level `name` (this is the `PARSER` value that will select it) and
implement `parse`:

```python
class EchoParser(DocumentParser):
    """Trivial example adapter — returns the raw bytes decoded as UTF-8.

    Real adapters convert their native output to normalized Markdown here.
    """

    name = "echo"

    def parse(self, raw: bytes, filename: str, content_type: str | None = None) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            # Surface a human-readable error → documents.error_message.
            raise ValueError(f"echo parser: not valid utf-8: {e}") from e
```

For a **cloud** adapter, take the HTTP client + API key in `__init__` as a
testable seam (so a unit test can inject an `httpx.MockTransport`), exactly like
`LlamaParseParser` / `CohereReranker` / `TavilyProvider`. See
[`backend/parsing.py`](../backend/parsing.py) `LlamaParseParser` as the **worked
reference** for a real commercial adapter (upload → poll job → fetch the
markdown result).

### Step 2 — Add the name to the allowed `PARSER` values

`PARSER` is validated in **two** places in `backend/parsing.py`, and a new
parser must be added to **both** (one is the runtime check, one keeps the type
hints honest so `mypy` stays green):

1. The `ParserName` type alias:

   ```python
   ParserName = Literal["docling", "unstructured", "llamaparse", "echo"]
   ```

2. The validation tuple inside `get_parser_name()` — this is what makes an
   unknown `PARSER` fail fast instead of silently defaulting to docling:

   ```python
   if raw not in ("docling", "unstructured", "llamaparse", "echo"):
       raise ValueError(
           f"PARSER must be one of docling|unstructured|llamaparse, got {raw!r}"
       )
   ```

### Step 3 — Register it in the `build_parser` factory

Add a branch to `build_parser(name)` (in `backend/parsing.py`) that returns your
adapter. **Commercial adapters must fail at build time on a missing key** — do
the key check here, mirroring the LlamaParse branch, so a misconfiguration
surfaces at boot, not on the first upload:

```python
def build_parser(name: ParserName) -> DocumentParser:
    if name == "echo":
        return EchoParser()
    if name == "docling":
        return DoclingParser()
    # ... llamaparse (build-time key check), unstructured (fails loudly) ...
```

> Never make an unknown / unimplemented value silently fall back to docling. The
> factory's whole job is fail-closed selection (ADR-0007).

### Step 4 — Select it

Set the env var (plus any API key your adapter needs):

```bash
PARSER=echo
# COMMERCIAL_API_KEY=...   # if your adapter requires one
```

`main.py` resolves `PARSER` once at startup via `get_selected_parser()`, so a
bad value or a missing commercial key fails **closed at boot**. The ingest path
then feeds `chunk_text(parser.parse(...))` with no parser-specific branch — your
Markdown output flows into the chunker exactly like docling's.

That's the whole path: **subclass → register (type + validation tuple +
factory) → `PARSER=<name>`.** No chunker, ingest-endpoint, or other backend
change is required.

## Prove the round-trip (don't just assert it)

A stub proves the wiring; only a real round-trip proves the *swap*. Use the
LlamaParse smoke test as your template — **[`backend/test_llamaparse_smoke.py`](../backend/test_llamaparse_smoke.py)
(US-041)**. Copy its shape:

- Drive the **full** `PARSER` env → `get_parser_name()` → `build_parser()` →
  adapter → output path (not a hand-built instance), so the test exercises the
  same wiring the ingest path uses.
- Parse a real fixture (e.g. `backend/test-fixtures/us018/sample.pdf`) through to
  a non-empty Markdown `str`, and assert recognizable content survives.
- For an adapter that needs an API key, **skip (exit 0) when the key is absent**
  rather than failing — so CI and contributors without an account stay green,
  while a keyed run proves the live round-trip. (The reranker and web-search
  suites take the same opt-in-by-key posture.)

For a **stubbed** unit test (no network, no key), copy
[`backend/test_llamaparse_parser.py`](../backend/test_llamaparse_parser.py): it
injects an `httpx.MockTransport` returning a canned response and asserts the
output is flattened Markdown, never a vendor-typed object.

## Unstructured.io: the canonical buyer-written adapter

[Unstructured.io](https://unstructured.io) is the named **canonical
buyer-written example** behind this protocol. The kit reserves `unstructured` as
an accepted `PARSER` value but ships **no** built-in implementation — selecting
it raises a loud `NotImplementedError` pointing here (it does **not** fall back
to docling). It is the obvious "bring your own adapter" target: write an
`UnstructuredParser(DocumentParser)` that calls the Unstructured API/SDK,
flatten its element list to Markdown at the boundary, and register it in
`build_parser` per the steps above.

Unstructured was a deliberate non-default: LlamaParse was chosen as the *shipped*
commercial adapter for the simplest API and strongest OCR / complex-layout
coverage — the gap docling cannot cover (ADR-0007 → Alternatives). Unstructured
remains a fully valid choice behind the same one-method protocol.

## F3 capability matrix

What the ingestion boundary covers, and the gaps it deliberately does **not** —
disclosed honestly so a buyer evaluating ingestion fidelity is never surprised.
"Tested" means exercised in CI / verified end-to-end. All rows are decided in
**ADR-0007**.

| Capability / target | Status | Notes |
| --- | --- | --- |
| **docling** default — text-based PDF / DOCX / HTML / MD → markdown (`.txt` verbatim) | ✅ **Tested** | The default (`PARSER=docling`); US-018 multi-format behavior. Verified by `backend/test_docling_parser.py`. ADR-0007. |
| **OCR / scanned / image-only** documents | ❌ **Out of scope for the default** | docling's default pipeline runs with OCR disabled (text-based only); an image-only PDF surfaces an explicit "no extractable text" error, never a silent empty ingest. Covered **only** by selecting the LlamaParse adapter. PRD §3.3 / ADR-0007. |
| **LlamaParse** adapter — OCR / complex-layout escape hatch | ✅ **Tested** (stubbed unit + keyed live smoke) | The one shipped commercial adapter (`PARSER=llamaparse` + `LLAMA_CLOUD_API_KEY`); requests the markdown result so tables/layout flatten server-side. `backend/test_llamaparse_parser.py` (stubbed) + `backend/test_llamaparse_smoke.py` (live, US-041). ADR-0007. |
| Boundary **output contract = markdown `str`** (v1) | ⚠️ **Markdown-string ceiling (pinned v1)** | Table structure / layout fidelity beyond what markdown preserves is **not** a v1 feature. Widening the boundary to carry structure (tables / bboxes / layout) for a layout-aware chunker is a documented **future** change that also touches `chunking.py` (US-045) — a deliberate pin, not an oversight. ADR-0007. |
| `PARSER=unstructured` | ❌ **Buyer-written adapter slot (not shipped)** | A named, accepted `PARSER` value with **no** built-in implementation; selecting it fails loudly (never falls back to docling). Write your own per this guide — LlamaParse is the one shipped commercial example. ADR-0007. |

## Checklist

- [ ] `class YourParser(DocumentParser)` with `name = "<value>"` and a
      `parse(raw, filename, content_type) -> str` that returns **normalized
      Markdown** (structured output flattened at the boundary).
- [ ] Added `"<value>"` to the `ParserName` `Literal` **and** the
      `get_parser_name()` validation tuple.
- [ ] Added a `build_parser` branch (commercial adapters: build-time key check).
- [ ] Selected via `PARSER=<value>` (+ API key if needed).
- [ ] A round-trip test modeled on US-041 (skips without a key; stubbed unit
      test modeled on `test_llamaparse_parser.py`).
- [ ] `parse` never returns a non-`str` / vendor-typed object (the v1 contract).
