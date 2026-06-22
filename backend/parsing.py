"""Multi-format document parsing for US-018.

Wraps `docling` so PDF / DOCX / HTML / MD all flow through the same
conversion → normalised Markdown → chunker path. `.txt` bypasses docling and
is returned verbatim after a utf-8 decode, because running plain text through
the heavyweight converter just adds latency without preserving any extra
structure.

Errors raised here are surfaced verbatim by the ingest endpoint as
`documents.error_message` (status=error), so messages are kept human-readable.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from io import BytesIO
from typing import Literal

import httpx
from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

log = logging.getLogger("agentic_rag.parsing")


class UnsupportedFormatError(ValueError):
    """Raised when neither the filename nor the content-type resolves to a
    format we know how to parse."""


class DocumentParser(ABC):
    """The ingestion parser boundary (ADR-0007, US-037).

    A parser turns raw uploaded bytes into normalized text ready for the
    chunker. Modeled on `Reranker` (`reranking.py`) / `WebSearchProvider`
    (`web_search.py`): a class-level `name` plus a single abstractmethod, so a
    reader who knows those two boundaries recognizes this one immediately.

    Contract (v1, pinned):

      * Input is `raw` bytes + the original `filename` + an optional
        `content_type` — the same triple the ingest path already holds and
        passes to `parse_document` today.
      * Output is a **normalized Markdown string**: headings as `#`,
        paragraphs separated by blank lines. The chunker (`chunk_text`,
        `chunking.py`) is Markdown/heading-aware only, so a parser whose
        native output is structured (tables / layout / bounding boxes) MUST
        flatten it to Markdown **here**, at the boundary — it must never leak
        a non-`str` type downstream. Widening the contract to carry structure
        for a layout-aware chunker is a documented future change (ADR-0007),
        not v1.

    Implementations set a class-level `name` matching their `PARSER` value
    (e.g. `"docling"`) and implement `parse`. Failures should surface as
    `UnsupportedFormatError` / `ValueError` (or an HTTP error for cloud
    adapters) so the ingest path can turn them into `documents.error_message`,
    consistent with the docling path today.
    """

    name: str

    @abstractmethod
    def parse(self, raw: bytes, filename: str, content_type: str | None = None) -> str:
        ...


_EXTENSION_TO_FORMAT: dict[str, InputFormat] = {
    ".pdf": InputFormat.PDF,
    ".docx": InputFormat.DOCX,
    ".html": InputFormat.HTML,
    ".htm": InputFormat.HTML,
    ".md": InputFormat.MD,
    ".markdown": InputFormat.MD,
}

# MIME fallback — the browser sometimes sends an empty `type` for .md, or
# `application/octet-stream` for drag-and-drop, so extension is the primary
# signal. This map only disambiguates when the extension is absent or unknown.
_MIME_TO_FORMAT: dict[str, InputFormat] = {
    "application/pdf": InputFormat.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": InputFormat.DOCX,
    "text/html": InputFormat.HTML,
    "text/markdown": InputFormat.MD,
    "text/x-markdown": InputFormat.MD,
}

_TEXT_EXTENSIONS = (".txt",)
_TEXT_MIMES = ("text/plain",)


def _make_converter() -> DocumentConverter:
    """Build a DocumentConverter covering the formats US-018 promises.

    OCR and table-structure inference on PDF are disabled: both require the
    heavy torch-based IBM models, and our pipeline targets text-based PDFs.
    Image-only PDFs will parse to empty content — surfaced as an explicit
    error in `parse_document` rather than silently ingesting nothing.
    """
    pdf_opts = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
    return DocumentConverter(
        allowed_formats=[
            InputFormat.PDF,
            InputFormat.DOCX,
            InputFormat.HTML,
            InputFormat.MD,
        ],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts),
        },
    )


# Instantiating DocumentConverter is expensive (loads backends, compiles
# regexes), so keep a module-level singleton shared across requests. The ingest
# path now offloads `parse` to an asyncio worker thread (`asyncio.to_thread`,
# main.py), so concurrent docling ingests would otherwise call `convert()` on
# this shared converter from multiple threads at once — docling's pipeline holds
# per-format model state and is not documented as thread-safe. `_convert_lock`
# serializes the docling conversion path so concurrent parses don't race on that
# state, preserving its prior effective serial behavior while the event loop
# stays free.
_converter: DocumentConverter | None = None
_convert_lock = threading.Lock()


def _get_converter() -> DocumentConverter:
    global _converter
    if _converter is None:
        _converter = _make_converter()
    return _converter


def _classify(filename: str, content_type: str | None) -> InputFormat | None:
    name = (filename or "").lower()
    for ext, fmt in _EXTENSION_TO_FORMAT.items():
        if name.endswith(ext):
            return fmt
    if content_type:
        fmt = _MIME_TO_FORMAT.get(content_type.split(";")[0].strip().lower())
        if fmt is not None:
            return fmt
    return None


def _is_plain_text(filename: str, content_type: str | None) -> bool:
    name = (filename or "").lower()
    if any(name.endswith(ext) for ext in _TEXT_EXTENSIONS):
        return True
    if content_type:
        head = content_type.split(";")[0].strip().lower()
        return head in _TEXT_MIMES
    return False


class DoclingParser(DocumentParser):
    """Default parser (US-038): today's docling conversion wrapped behind the
    `DocumentParser` boundary. The first concrete adapter, selected by the
    default `PARSER=docling`; behavior is unchanged from the original
    `parse_document`.

    Routing (US-018, preserved):
      * `.txt` / `text/plain` → utf-8 decode, return as-is (bypasses docling —
        plain text through the heavy converter adds latency without preserving
        any extra structure).
      * `.pdf`, `.docx`, `.html`, `.md` → docling → `export_to_markdown()`.
      * Anything else → `UnsupportedFormatError`.

    The `pypdfium2` PDF fallback lives **inside** this adapter (an ADR-0007
    docling-path concern, not a boundary concern): when docling's torch-based
    PDF pipeline is unavailable, text-based PDFs still extract via pypdfium2.
    Output is Markdown (headings as `#`, paragraphs separated by blank lines)
    so the chunker can respect structural boundaries.
    """

    name = "docling"

    def parse(self, raw: bytes, filename: str, content_type: str | None = None) -> str:
        if _is_plain_text(filename, content_type):
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ValueError(f"file is not valid utf-8: {e}") from e

        fmt = _classify(filename, content_type)
        if fmt is None:
            raise UnsupportedFormatError(
                f"unsupported file type for {filename!r} (content-type={content_type!r}); "
                "accepted: .pdf, .docx, .html, .md, .txt"
            )

        with _convert_lock:
            try:
                stream = DocumentStream(name=filename or f"document{_default_suffix(fmt)}", stream=BytesIO(raw))
                result = _get_converter().convert(stream)
                text = result.document.export_to_markdown().strip()
            except Exception as e:  # noqa: BLE001 — see fallback below
                # docling's PDF pipeline depends on torch-based layout models. When
                # torch<2.4 is installed (some older base images / dev venvs), the
                # layout step blows up and the whole conversion errors out. For PDFs
                # specifically, fall back to pypdfium2 raw text extraction so ingestion
                # still works on text-based PDFs — at the cost of losing heading
                # structure docling would have reconstructed. Other formats rethrow.
                if fmt is InputFormat.PDF:
                    log.warning("docling PDF pipeline failed (%s); falling back to pypdfium2", e)
                    text = _pdf_text_fallback(raw).strip()
                else:
                    raise ValueError(f"failed to parse {fmt.name.lower()}: {e}") from e

        if not text:
            raise ValueError(
                f"{fmt.name.lower()} produced no extractable text — "
                f"image-only PDFs / empty documents are not supported"
            )
        return text


# LlamaParse cloud parsing API. `parse` is synchronous (the DocumentParser
# contract), so we use a sync `httpx.Client` and bound the job polling. 60s per
# HTTP request is generous for an upload / status / result fetch; total wait is
# bounded by `max_polls * poll_interval` below.
_LLAMAPARSE_TIMEOUT = httpx.Timeout(60.0)


class LlamaParseParser(DocumentParser):
    """LlamaParse cloud adapter (US-040 / I6) — the OCR / scanned / complex-
    layout escape hatch docling's default text pipeline cannot cover
    (PRD §3.3). Shipped as the one real commercial adapter (chosen over
    Unstructured.io for the simplest API + strongest OCR coverage — the gap
    docling can't reach; ADR-0007 alternatives).

    Calls the LlamaCloud parsing API (Bearer `LLAMA_CLOUD_API_KEY`) and requests
    the **markdown** result, so LlamaParse flattens tables / layout to markdown
    server-side: the v1 boundary contract (a markdown `str`) is honored here, at
    the boundary — a LlamaParse-typed / structured object never leaks downstream
    to the chunker.

    The constructor injects the HTTP client + key as a testable seam, matching
    `CohereReranker` (`reranking.py`) / `TavilyProvider` (`web_search.py`); they
    take an `AsyncClient` because their calls are async, this takes a sync
    `httpx.Client` because `parse` is synchronous. The stubbed unit test
    (`test_llamaparse_parser.py`) drives it with an `httpx.MockTransport`; the
    live round-trip is proven by the keyed US-041 smoke test.

    LlamaParse's REST flow is asynchronous on its side — `upload` returns a job
    id, the job is polled to a terminal status, then the markdown result is
    fetched. `parse` blocks the same way the docling path does today.
    """

    name = "llamaparse"
    BASE_URL = "https://api.cloud.llamaindex.ai/api/v1/parsing"
    # Terminal job states from the LlamaParse API. PARTIAL_SUCCESS still yields
    # usable markdown (some pages parsed), so it is treated as a success.
    _SUCCESS_STATES = ("SUCCESS", "PARTIAL_SUCCESS")
    _FAILURE_STATES = ("ERROR", "CANCELED")

    def __init__(
        self,
        http: httpx.Client,
        api_key: str,
        *,
        poll_interval: float = 2.0,
        max_polls: int = 150,
    ) -> None:
        self.http = http
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.max_polls = max_polls

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json",
        }

    def parse(self, raw: bytes, filename: str, content_type: str | None = None) -> str:
        job_id, status = self._upload(raw, filename, content_type)
        self._await_terminal(job_id, status)
        markdown = self._fetch_markdown(job_id)
        if not markdown.strip():
            # Mirror the docling adapter's empty-output guard: an image-only /
            # empty document yields nothing useful — surface it, don't ingest a
            # blank doc. (LlamaParse's OCR covers most scans, but not all.)
            raise ValueError(
                "LlamaParse produced no extractable text — image-only or empty "
                "documents may not be parseable"
            )
        return markdown.strip()

    def _upload(
        self, raw: bytes, filename: str, content_type: str | None
    ) -> tuple[str, str]:
        files = {
            "file": (
                filename or "document",
                raw,
                content_type or "application/octet-stream",
            )
        }
        r = self.http.post(
            f"{self.BASE_URL}/upload", headers=self._headers(), files=files
        )
        r.raise_for_status()
        body = r.json()
        job_id = body.get("id")
        if not job_id:
            raise ValueError(f"LlamaParse upload returned no job id: {body!r}")
        return job_id, (body.get("status") or "PENDING").upper()

    def _await_terminal(self, job_id: str, status: str) -> None:
        """Poll the job to a terminal state. Raises on failure / timeout."""
        status = (status or "").upper()
        for poll in range(self.max_polls + 1):
            if status in self._SUCCESS_STATES:
                return
            if status in self._FAILURE_STATES:
                raise ValueError(
                    f"LlamaParse job {job_id} ended with status {status}"
                )
            if poll == self.max_polls:
                break
            time.sleep(self.poll_interval)
            r = self.http.get(
                f"{self.BASE_URL}/job/{job_id}", headers=self._headers()
            )
            r.raise_for_status()
            status = (r.json().get("status") or "").upper()
        raise ValueError(
            f"LlamaParse job {job_id} did not finish within {self.max_polls} "
            f"polls (~{self.max_polls * self.poll_interval:.0f}s)"
        )

    def _fetch_markdown(self, job_id: str) -> str:
        r = self.http.get(
            f"{self.BASE_URL}/job/{job_id}/result/markdown", headers=self._headers()
        )
        r.raise_for_status()
        body = r.json()
        markdown = body.get("markdown")
        if markdown is None:
            # Some result payloads carry per-page markdown under `pages[].md`
            # instead of a top-level `markdown` — join them in page order.
            pages = body.get("pages") or []
            markdown = "\n\n".join(
                (p.get("md") or "") for p in pages if isinstance(p, dict)
            )
        if not isinstance(markdown, str):
            raise ValueError(
                f"LlamaParse markdown result was not a string "
                f"(got {type(markdown).__name__}) — boundary contract is markdown str"
            )
        return markdown


# ---- Parser selection (US-039, ADR-0007) ----------------------------------
# `PARSER` chooses the ingestion parser; `build_parser` mirrors `build_reranker`
# (`reranking.py`) / `build_web_search_provider` (`web_search.py`) so swapping
# the parser is a config switch (+ an API key for commercial adapters).

ParserName = Literal["docling", "unstructured", "llamaparse"]


def get_parser_name() -> ParserName:
    """`PARSER` env: `docling` (default) | `unstructured` | `llamaparse`.

    Validates eagerly and raises a clear `ValueError` on an unknown value —
    same shape as `get_reranker_name()` — so a typo can never silently fall
    back to docling.
    """
    raw = (os.environ.get("PARSER") or "docling").strip().lower()
    if raw not in ("docling", "unstructured", "llamaparse"):
        raise ValueError(
            f"PARSER must be one of docling|unstructured|llamaparse, got {raw!r}"
        )
    return raw  # type: ignore[return-value]


def build_parser(name: ParserName) -> DocumentParser:
    """Factory matching `PARSER` to a concrete `DocumentParser` (ADR-0007).

    Commercial adapters raise on a missing API key **at build time** so config
    mistakes surface immediately rather than at first ingest — mirroring the
    build-time key checks in `build_reranker` / `build_web_search_provider`. An
    accepted-but-unshipped selection fails loudly here; it never silently falls
    back to docling.
    """
    if name == "docling":
        return DoclingParser()
    if name == "llamaparse":
        api_key = os.environ.get("LLAMA_CLOUD_API_KEY")
        if not api_key:
            raise ValueError("PARSER=llamaparse requires LLAMA_CLOUD_API_KEY")
        # US-040: the real LlamaParse adapter (I6). The sync client is long-lived
        # alongside the cached parser singleton (get_selected_parser), so it is
        # not closed per-call. Constructing the client makes no network request,
        # so a keyed selection succeeds here and the API is only hit on ingest.
        return LlamaParseParser(
            http=httpx.Client(timeout=_LLAMAPARSE_TIMEOUT), api_key=api_key
        )
    if name == "unstructured":
        # A named, accepted "bring your own adapter" slot (US-043) — no built-in
        # implementation ships (LlamaParse is the one shipped commercial
        # adapter). Fail loudly; never silently fall back to docling.
        raise NotImplementedError(
            "PARSER=unstructured has no built-in adapter — write your own "
            "DocumentParser (see the US-043 authoring guide), or use "
            "PARSER=docling|llamaparse"
        )
    raise ValueError(f"unhandled parser name: {name}")  # pragma: no cover


_selected_parser: DocumentParser | None = None


def get_selected_parser() -> DocumentParser:
    """Return the process-wide `PARSER`-selected parser, built once and cached.

    `main.py` calls this at startup so a misconfigured `PARSER` (unknown value,
    or a commercial adapter with no API key) fails **closed at boot** — never
    deferred to the first upload. Default `PARSER=docling` preserves today's
    behavior exactly.
    """
    global _selected_parser
    if _selected_parser is None:
        _selected_parser = build_parser(get_parser_name())
    return _selected_parser


def reset_selected_parser() -> None:
    """Drop the cached selection — for tests that vary `PARSER` across cases."""
    global _selected_parser
    _selected_parser = None


def parse_document(
    raw: bytes,
    filename: str,
    content_type: str | None = None,
) -> str:
    """Back-compat docling entry (US-018 / US-038): parse `raw` to Markdown
    with **docling specifically**, regardless of `PARSER`. Retained for callers
    and tests that want the default parser explicitly; the ingest path uses the
    `PARSER`-selected parser (`get_selected_parser`, US-039).
    """
    return DoclingParser().parse(raw, filename, content_type)


def _pdf_text_fallback(raw: bytes) -> str:
    """Last-resort PDF text extraction via pypdfium2. Preserves page order and
    inserts blank-line separators between pages so the chunker still sees a
    block structure.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(raw)
    try:
        pages: list[str] = []
        for page in pdf:
            textpage = page.get_textpage()
            try:
                pages.append(textpage.get_text_range() or "")
            finally:
                textpage.close()
        return "\n\n".join(p.strip() for p in pages if p.strip())
    finally:
        pdf.close()


def _default_suffix(fmt: InputFormat) -> str:
    return {
        InputFormat.PDF: ".pdf",
        InputFormat.DOCX: ".docx",
        InputFormat.HTML: ".html",
        InputFormat.MD: ".md",
    }.get(fmt, "")


# Module-level cold-start guard: lazily loading docling on the first ingest
# would push a user-facing request into multi-second init. Callers can call
# this at boot to front-load the cost.
def warmup() -> None:
    if os.environ.get("SKIP_DOCLING_WARMUP"):
        return
    if get_parser_name() != "docling":
        return
    try:
        _get_converter()
    except Exception:  # noqa: BLE001 — warmup must never kill the process
        log.exception("docling warmup failed; first ingest will pay the cost")
