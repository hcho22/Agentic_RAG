"""US-042 validation test: selected-parser output feeds `chunk_text` unchanged.

Proves the markdown-`str` contract — not the parser identity — is the only
coupling between the ingestion boundary (`DocumentParser.parse`) and the chunker
(`chunk_text`, `chunking.py:109`):

  1. `chunk_text(DoclingParser().parse(<sample.md>))` → non-empty `list[str]`.
  2. `chunk_text(LlamaParseParser(<stub>).parse(<sample.pdf>))` → non-empty
     `list[str]` (stubbed transport — no key / network).
  3. Both outputs flow through the SAME `chunk_text` call — the test chunks each
     parser's output via one helper with no `isinstance` / `name ==` branch on
     the parser, mirroring the parser-agnostic ingest path (`main.py`).

This is the load-bearing proof that swapping `PARSER` needs no chunker change.

Run:
    python -m backend.test_parser_chunker_contract
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from chunking import chunk_text  # noqa: E402
from parsing import DoclingParser, DocumentParser, LlamaParseParser  # noqa: E402

FIXTURE_DIR = HERE / "test-fixtures" / "us018"
JOB_ID = "job-contract-1"

# A multi-section markdown doc — what a LlamaParse markdown result looks like
# after server-side flattening (heading hierarchy + a table). Non-empty is the
# bar; the structure just makes the chunker's job realistic.
CANNED_MARKDOWN = """# Ingestion Boundary Sample

This is a US-042 contract document: a parser's markdown output must feed the
chunker with no parser-specific handling.

## Second Section

Docling and LlamaParse both flatten to markdown at the boundary, so the chunker
stays format-agnostic and parser-agnostic.

| Parser | Output |
| ------ | ------ |
| docling | markdown str |
| llamaparse | markdown str |
"""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _raw(name: str) -> bytes:
    path = FIXTURE_DIR / name
    _check(path.exists(), f"missing fixture {path} — regenerate test-fixtures/us018/")
    return path.read_bytes()


def _llamaparse_stub() -> LlamaParseParser:
    """A `LlamaParseParser` backed by an `httpx.MockTransport` returning a canned
    markdown result — no key, no network (same posture as the US-040 test)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/parsing/upload"):
            return httpx.Response(200, json={"id": JOB_ID, "status": "PENDING"})
        if path.endswith(f"/parsing/job/{JOB_ID}/result/markdown"):
            return httpx.Response(200, json={"markdown": CANNED_MARKDOWN})
        if path.endswith(f"/parsing/job/{JOB_ID}"):
            return httpx.Response(200, json={"status": "SUCCESS"})
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LlamaParseParser(http=client, api_key="test-key", poll_interval=0)


def _chunk_via(parser: DocumentParser, raw: bytes, filename: str, content_type: str) -> list[str]:
    """Parse then chunk with NO branch on which parser produced the text — the
    single code path the ingest pipeline uses for every adapter."""
    return chunk_text(parser.parse(raw, filename, content_type))


def _assert_non_empty_str_list(chunks: object, who: str) -> None:
    _check(isinstance(chunks, list), f"{who}: chunk_text must return a list, got {type(chunks).__name__}")
    assert isinstance(chunks, list)  # narrow for the checks below
    _check(len(chunks) >= 1, f"{who}: chunk_text must yield a non-empty chunk list")
    _check(
        all(isinstance(c, str) and c.strip() for c in chunks),
        f"{who}: every chunk must be a non-empty str",
    )


def test_docling_output_feeds_chunker() -> None:
    chunks = _chunk_via(DoclingParser(), _raw("sample.md"), "sample.md", "text/markdown")
    _assert_non_empty_str_list(chunks, "docling")
    _check(
        any("Ingestion Boundary Sample" in c for c in chunks),
        "docling chunks must carry the parsed markdown content",
    )
    print(f"ok: DoclingParser markdown -> chunk_text -> {len(chunks)} non-empty chunk(s)")


def test_llamaparse_output_feeds_chunker() -> None:
    chunks = _chunk_via(_llamaparse_stub(), _raw("sample.pdf"), "sample.pdf", "application/pdf")
    _assert_non_empty_str_list(chunks, "llamaparse")
    _check(
        any("Ingestion Boundary Sample" in c for c in chunks),
        "llamaparse chunks must carry the parsed markdown content",
    )
    print(f"ok: LlamaParseParser markdown -> chunk_text -> {len(chunks)} non-empty chunk(s)")


def test_both_parsers_use_one_identical_chunk_call() -> None:
    """The crux of US-042: a single, parser-agnostic call site chunks both
    adapters' output. If the chunker needed to know the parser, this loop
    couldn't be parser-blind."""
    cases = (
        (DoclingParser(), _raw("sample.md"), "sample.md", "text/markdown"),
        (_llamaparse_stub(), _raw("sample.pdf"), "sample.pdf", "application/pdf"),
    )
    for parser, raw, name, ct in cases:
        chunks = _chunk_via(parser, raw, name, ct)  # identical call for every parser
        _assert_non_empty_str_list(chunks, parser.name)
    print("ok: both parsers chunk through one identical parser-agnostic call (no isinstance/name== branch)")


def main() -> int:
    tests = [
        test_docling_output_feeds_chunker,
        test_llamaparse_output_feeds_chunker,
        test_both_parsers_use_one_identical_chunk_call,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} parser→chunker contract (US-042) checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
