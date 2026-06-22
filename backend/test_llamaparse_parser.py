"""US-040 validation test: the `LlamaParseParser` adapter (I6).

Proves the shipped commercial adapter satisfies the `DocumentParser` boundary
without a live key — driven by an `httpx.MockTransport` stub that returns a
canned LlamaParse markdown response:

  1. `LlamaParseParser(http=<stub>, api_key="test")` constructs with no network
     call (the testable seam — http + key injected, like CohereReranker).
  2. `.parse(<pdf bytes>, "sample.pdf", "application/pdf")` runs the real REST
     flow (upload → poll job → fetch markdown result) against the stub and
     returns a markdown `str` — structured fields in the stub (a table) come
     back as markdown table syntax, not a non-`str` / LlamaParse-typed object.
  3. The Bearer key is sent and the `result/markdown` endpoint (not raw JSON)
     is the one requested — the flattening-at-the-boundary contract.
  4. A job that ends in `ERROR`, and an empty markdown result, both raise
     `ValueError` (so the ingest path can surface `documents.error_message`).
  5. The factory returns a `LlamaParseParser` when `PARSER=llamaparse` + a key
     is set — and never a `DoclingParser`.

The live round-trip against the real API is the separate US-041 smoke test;
this test needs no network and no key.

Run:
    python -m backend.test_llamaparse_parser
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from parsing import (  # noqa: E402
    DocumentParser,
    LlamaParseParser,
    build_parser,
    reset_selected_parser,
)

JOB_ID = "job-abc-123"

# A canned LlamaParse markdown result carrying structure (heading + a table) —
# i.e. what LlamaParse returns server-side already flattened to markdown when
# the markdown result type is requested.
CANNED_MARKDOWN = """# Ingestion Boundary Sample

Some prose describing the document.

| Region | Revenue |
| ------ | ------- |
| North  | 100     |
| South  | 200     |
"""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class _Recorder:
    """Records each request path so a test can assert the upload → poll →
    result flow ran and the right (markdown, not json) endpoint was hit."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.auth_headers: list[str | None] = []

    def transport(self, *, status: str = "SUCCESS", markdown=CANNED_MARKDOWN):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.paths.append(path)
            self.auth_headers.append(request.headers.get("authorization"))
            if path.endswith("/parsing/upload"):
                return httpx.Response(200, json={"id": JOB_ID, "status": "PENDING"})
            if path.endswith(f"/parsing/job/{JOB_ID}/result/markdown"):
                return httpx.Response(200, json={"markdown": markdown})
            if path.endswith(f"/parsing/job/{JOB_ID}"):
                return httpx.Response(200, json={"id": JOB_ID, "status": status})
            return httpx.Response(404, json={"error": f"unexpected path {path}"})

        return httpx.MockTransport(handler)


def _parser(rec: _Recorder, **transport_kwargs) -> LlamaParseParser:
    client = httpx.Client(transport=rec.transport(**transport_kwargs))
    # poll_interval=0 so the upload→poll→result flow runs with no wall-clock wait.
    return LlamaParseParser(http=client, api_key="test-key", poll_interval=0)


def test_is_documentparser_named_llamaparse() -> None:
    p = _parser(_Recorder())
    _check(p.name == "llamaparse", f"name must be 'llamaparse', got {p.name!r}")
    _check(isinstance(p, DocumentParser), "LlamaParseParser must be a DocumentParser")
    print("ok: LlamaParseParser is a concrete DocumentParser named 'llamaparse'")


def test_constructs_with_stub_no_network() -> None:
    """Construction injects http + key and makes no network call (testable seam)."""
    rec = _Recorder()
    _parser(rec)  # building must not hit the transport
    _check(rec.paths == [], "constructing LlamaParseParser must make no HTTP request")
    print("ok: LlamaParseParser constructs from a stub with no network call")


def test_parse_returns_flattened_markdown() -> None:
    rec = _Recorder()
    out = _parser(rec).parse(b"%PDF-1.4 fake", "sample.pdf", "application/pdf")
    _check(isinstance(out, str), f"parse must return str, got {type(out).__name__}")
    _check(bool(out.strip()), "parse must return non-empty markdown")
    _check(
        any(line.lstrip().startswith("#") for line in out.splitlines()),
        f"output must contain a markdown heading, got:\n{out}",
    )
    _check("| Region | Revenue |" in out, "structured table must be flattened to markdown table syntax")
    _check("Ingestion Boundary Sample" in out, "output must carry the fixture content")
    print("ok: .parse() returns a flattened markdown str (heading + table syntax)")


def test_full_flow_upload_poll_result() -> None:
    """The adapter runs upload → poll job status → fetch the markdown result."""
    rec = _Recorder()
    _parser(rec).parse(b"data", "sample.pdf", "application/pdf")
    _check(any(p.endswith("/parsing/upload") for p in rec.paths), "must POST /upload")
    _check(
        any(p.endswith(f"/parsing/job/{JOB_ID}") for p in rec.paths),
        "must poll the job status endpoint",
    )
    _check(
        any(p.endswith("/result/markdown") for p in rec.paths),
        "must fetch the MARKDOWN result (not raw JSON) — flatten-at-boundary contract",
    )
    _check(
        all(h == "Bearer test-key" for h in rec.auth_headers),
        f"every call must carry the Bearer key, got {rec.auth_headers}",
    )
    print("ok: parse runs upload -> poll -> result/markdown with Bearer auth")


def test_job_error_raises_valueerror() -> None:
    rec = _Recorder()
    try:
        _parser(rec, status="ERROR").parse(b"data", "sample.pdf", "application/pdf")
    except ValueError as e:
        _check("ERROR" in str(e), f"error must name the failed status, got: {e}")
    else:
        raise AssertionError("a job ending in ERROR must raise ValueError")
    print("ok: a failed LlamaParse job raises ValueError (surfaced as error_message)")


def test_empty_markdown_raises_valueerror() -> None:
    rec = _Recorder()
    try:
        _parser(rec, markdown="   ").parse(b"data", "scan.pdf", "application/pdf")
    except ValueError as e:
        _check(
            "no extractable text" in str(e),
            f"empty result must explain it, got: {e}",
        )
    else:
        raise AssertionError("empty markdown result must raise ValueError")
    print("ok: an empty markdown result raises ValueError (no blank-doc ingest)")


def test_factory_returns_llamaparse_with_key() -> None:
    """build_parser('llamaparse') + a key returns LlamaParseParser, never docling."""
    saved = os.environ.get("LLAMA_CLOUD_API_KEY")
    try:
        os.environ["LLAMA_CLOUD_API_KEY"] = "test-key"
        reset_selected_parser()
        parser = build_parser("llamaparse")
        _check(
            isinstance(parser, LlamaParseParser),
            f"build_parser('llamaparse') must return LlamaParseParser, got {type(parser).__name__}",
        )
        _check(parser.name == "llamaparse", "factory parser name must be 'llamaparse'")
    finally:
        if saved is None:
            os.environ.pop("LLAMA_CLOUD_API_KEY", None)
        else:
            os.environ["LLAMA_CLOUD_API_KEY"] = saved
        reset_selected_parser()
    print("ok: build_parser('llamaparse') + key returns a LlamaParseParser (no docling fallback)")


def main() -> int:
    tests = [
        test_is_documentparser_named_llamaparse,
        test_constructs_with_stub_no_network,
        test_parse_returns_flattened_markdown,
        test_full_flow_upload_poll_result,
        test_job_error_raises_valueerror,
        test_empty_markdown_raises_valueerror,
        test_factory_returns_llamaparse_with_key,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} LlamaParseParser (US-040) checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
