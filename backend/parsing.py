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
from io import BytesIO

from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

log = logging.getLogger("agentic_rag.parsing")


class UnsupportedFormatError(ValueError):
    """Raised when neither the filename nor the content-type resolves to a
    format we know how to parse."""


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
# regexes). Keep a module-level singleton — the converter is stateless across
# `convert()` calls so sharing across requests is safe.
_converter: DocumentConverter | None = None


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


def parse_document(
    raw: bytes,
    filename: str,
    content_type: str | None = None,
) -> str:
    """Convert `raw` bytes to normalised text ready for chunking.

    Routing:
      * `.txt` / `text/plain` → utf-8 decode, return as-is.
      * `.pdf`, `.docx`, `.html`, `.md` → docling → export_to_markdown().
      * Anything else → `UnsupportedFormatError`.

    Output is Markdown (headings as `#`, paragraphs separated by blank lines)
    so the downstream chunker can respect structural boundaries — US-018
    requirement "doesn't split mid-heading".
    """
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
    try:
        _get_converter()
    except Exception:  # noqa: BLE001 — warmup must never kill the process
        log.exception("docling warmup failed; first ingest will pay the cost")
