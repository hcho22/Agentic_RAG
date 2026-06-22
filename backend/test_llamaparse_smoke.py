"""US-041 smoke test: a REAL LlamaParse round-trip (I6).

Proves "the swap works" end-to-end against the live API — a stub (US-040)
cannot prove I6. The full chain is exercised:

    PARSER=llamaparse env → get_parser_name() → build_parser() →
    LlamaParseParser → live LlamaCloud API → normalized markdown str →
    ready for chunk_text

Opt-in by key, mirroring the reranker / web-search live suites:

  * `LLAMA_CLOUD_API_KEY` set  → runs the real round-trip and asserts the
    fixture comes back as non-empty markdown containing recognizable content.
  * `LLAMA_CLOUD_API_KEY` absent → **skips** (exit 0, not a failure), so CI and
    contributors without a LlamaParse account stay green.

Run it locally with a key (one-liner):

    LLAMA_CLOUD_API_KEY=llx-... PARSER=llamaparse python -m backend.test_llamaparse_smoke

(or from inside `backend/`: `... python -m test_llamaparse_smoke`). The key may
also live in `backend/.env`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# `.env` is a convenience — a directly-exported key works too. Guard the import
# so the smoke test never hard-fails just because python-dotenv is absent.
try:
    from dotenv import load_dotenv

    load_dotenv(HERE / ".env")
except ImportError:  # pragma: no cover - dotenv is a normal dep
    pass

# Skip cleanly without a key BEFORE importing/building anything — keeps CI green
# for contributors without a LlamaParse account; a keyed run proves the live
# round-trip. (Build would otherwise raise on the missing key by design.)
if not os.environ.get("LLAMA_CLOUD_API_KEY"):
    print("SKIP: LLAMA_CLOUD_API_KEY not set — live LlamaParse round-trip skipped")
    sys.exit(0)

from parsing import (  # noqa: E402
    LlamaParseParser,
    build_parser,
    get_parser_name,
    reset_selected_parser,
)

FIXTURE = HERE / "test-fixtures" / "us018" / "sample.pdf"
# The fixture's title line — the most reliably-extracted recognizable content
# (see test-fixtures/us018/sample.txt).
RECOGNIZABLE = "Ingestion Boundary Sample"


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace so the content check tolerates the live
    API reflowing headings / spacing differently than the source PDF."""
    return " ".join(s.lower().split())


def test_live_round_trip() -> None:
    _check(FIXTURE.exists(), f"missing fixture {FIXTURE} — regenerate test-fixtures/us018/")

    # Drive the full env → factory → adapter path, not a hand-built instance, so
    # the smoke test proves the same wiring the ingest path uses.
    saved_parser = os.environ.get("PARSER")
    try:
        os.environ["PARSER"] = "llamaparse"
        reset_selected_parser()
        parser = build_parser(get_parser_name())
        _check(
            isinstance(parser, LlamaParseParser) and parser.name == "llamaparse",
            f"PARSER=llamaparse must build a LlamaParseParser, got {type(parser).__name__}",
        )

        text = parser.parse(
            FIXTURE.read_bytes(), filename="sample.pdf", content_type="application/pdf"
        )
    finally:
        if saved_parser is None:
            os.environ.pop("PARSER", None)
        else:
            os.environ["PARSER"] = saved_parser
        reset_selected_parser()

    _check(isinstance(text, str), f"live parse must return str, got {type(text).__name__}")
    _check(bool(text.strip()), "live LlamaParse round-trip must return non-empty markdown")
    _check(
        _normalize(RECOGNIZABLE) in _normalize(text),
        f"live result must contain fixture content {RECOGNIZABLE!r}; got:\n{text[:500]}",
    )
    print(
        f"ok: live LlamaParse round-trip returned {len(text)} chars of markdown "
        f"containing {RECOGNIZABLE!r}"
    )
    print(
        "    (PARSER env -> build_parser -> LlamaParseParser -> live API -> "
        "markdown str -> ready for chunk_text)"
    )


def main() -> int:
    test_live_round_trip()
    print("\nPASS: 1 LlamaParse live round-trip (US-041) check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
