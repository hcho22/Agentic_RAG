"""US-038 validation test: `DoclingParser` is the default `DocumentParser`.

Proves today's docling logic, wrapped behind the boundary, is behavior-
preserving (US-018 multi-format support intact) and that `parse_document`
delegates to it:

  1. `DoclingParser().name == "docling"` and it is a concrete `DocumentParser`.
  2. Each US-018 fixture (`.pdf/.docx/.html/.md/.txt`) parses to a non-empty
     Markdown string, and `DoclingParser().parse(...)` == legacy
     `parse_document(...)` byte-for-byte (the wrapper delegates).
  3. `.txt` is returned verbatim (bypasses docling).
  4. Markdown structure survives for `.md/.html/.docx` (heading markers kept).
  5. `.pdf` extracts text — exercising the in-adapter `pypdfium2` fallback when
     docling's torch pipeline is unavailable (the fallback the failure
     indicator warns must keep triggering).
  6. An unknown extension still raises `UnsupportedFormatError`.

Run:
    python -m backend.test_docling_parser
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from parsing import (  # noqa: E402
    DocumentParser,
    DoclingParser,
    UnsupportedFormatError,
    parse_document,
)

FIXTURE_DIR = HERE / "test-fixtures" / "us018"

# (filename -> content_type) mirroring what the browser sends on upload.
FIXTURES = {
    "sample.txt": "text/plain",
    "sample.md": "text/markdown",
    "sample.html": "text/html",
    "sample.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "sample.pdf": "application/pdf",
}
# A phrase present in every fixture (see the fixture generator).
RECOGNIZABLE = "Ingestion Boundary Sample"


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _raw(name: str) -> bytes:
    path = FIXTURE_DIR / name
    _check(path.exists(), f"missing fixture {path} — regenerate test-fixtures/us018/")
    return path.read_bytes()


def test_name_and_is_concrete_parser() -> None:
    parser = DoclingParser()
    _check(parser.name == "docling", f"name must be 'docling', got {parser.name!r}")
    _check(isinstance(parser, DocumentParser), "DoclingParser must be a DocumentParser")
    print("ok: DoclingParser() is a concrete DocumentParser named 'docling'")


def test_each_fixture_matches_legacy_and_is_markdown() -> None:
    parser = DoclingParser()
    for name, ct in FIXTURES.items():
        raw = _raw(name)
        out = parser.parse(raw, name, ct)
        legacy = parse_document(raw, name, ct)
        _check(isinstance(out, str) and out.strip(), f"{name}: parse must return non-empty str")
        _check(out == legacy, f"{name}: DoclingParser.parse must equal legacy parse_document output")
        _check(RECOGNIZABLE in out, f"{name}: parsed text must contain fixture content {RECOGNIZABLE!r}")
    print(f"ok: {len(FIXTURES)} US-018 fixtures parse to non-empty markdown == legacy parse_document")


def test_txt_returned_verbatim() -> None:
    raw = _raw("sample.txt")
    out = DoclingParser().parse(raw, "sample.txt", "text/plain")
    _check(out == raw.decode("utf-8"), ".txt must be returned verbatim (utf-8 decode, no docling)")
    print("ok: .txt is returned verbatim")


def test_markdown_structure_preserved() -> None:
    """`.md/.html/.docx` keep heading structure (markdown `#`), so the chunker
    can split on boundaries — the 'markdown structure lost' failure indicator."""
    parser = DoclingParser()
    for name in ("sample.md", "sample.html", "sample.docx"):
        out = parser.parse(_raw(name), name, FIXTURES[name])
        _check(
            any(line.lstrip().startswith("#") for line in out.splitlines()),
            f"{name}: expected a markdown heading (`#`) in output, got:\n{out}",
        )
    print("ok: .md/.html/.docx preserve markdown heading structure")


def test_pdf_extracts_text_via_fallback() -> None:
    """`.pdf` yields non-empty text. In a torch<2.4 env docling's PDF pipeline
    is unavailable, so this exercises the in-adapter pypdfium2 fallback; with a
    full torch it goes through docling. Either way text must come out."""
    out = DoclingParser().parse(_raw("sample.pdf"), "sample.pdf", "application/pdf")
    _check(isinstance(out, str) and out.strip(), "PDF must parse to non-empty text")
    _check(RECOGNIZABLE in out, f"PDF text must contain fixture content {RECOGNIZABLE!r}")
    print("ok: .pdf extracts non-empty text (pypdfium2 fallback triggers when torch is absent)")


def test_unknown_extension_raises() -> None:
    for call in (
        lambda: DoclingParser().parse(b"data", "mystery.xyz", "application/x-other"),
        lambda: parse_document(b"data", "mystery.xyz", "application/x-other"),
    ):
        try:
            call()
        except UnsupportedFormatError:
            continue
        raise AssertionError("unknown extension must raise UnsupportedFormatError")
    print("ok: unknown extension raises UnsupportedFormatError (adapter + wrapper)")


def main() -> int:
    tests = [
        test_name_and_is_concrete_parser,
        test_each_fixture_matches_legacy_and_is_markdown,
        test_txt_returned_verbatim,
        test_markdown_structure_preserved,
        test_pdf_extracts_text_via_fallback,
        test_unknown_extension_raises,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} DoclingParser (US-038) checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
