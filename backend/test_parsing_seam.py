"""US-036 validation test: the docling ingestion seam is clean (I5).

A programmatic, CI-runnable form of the ADR-0007 / PRD US-036 verification
greps. It proves three things about the ingestion parser boundary:

  1. The docling library is confined to exactly ONE backend module —
     `parsing.py`. No other `backend/*.py` imports docling or names a
     docling-only type, so nothing docling-typed escapes the seam.
  2. `chunking.py` carries no docling reference — the chunker is parser-
     agnostic and consumes only normalized markdown text.
  3. `chunk_text`'s document argument is typed `str`, the pinned v1 output
     contract of the boundary.

This is what lets US-037–045 swap the parser (the `DocumentParser` protocol +
`build_parser` factory, the LlamaParse adapter) without touching the chunker.
The test fails loudly if docling ever leaks past `parsing.py`.

The docling trigger tokens below are deliberately assembled from fragments so
this test file does not itself match the US-036 grep, which scans every
`backend/*.py` — this file included.

Run:
    python -m backend.test_parsing_seam
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent

# The library name and its boundary-only symbols, assembled from fragments so
# this file stays grep-clean (see the module docstring). These mirror the
# alternation in the PRD US-036 / ADR-0007 validation grep exactly.
_LIB = "docling"
_IMPORT_TOKENS = (
    "import " + _LIB,
    "from " + _LIB,
    "Document" + "Converter",
    "Input" + "Format",
    "Document" + "Stream",
)
# What chunking.py must never mention (bare library name + its leaf types).
_CHUNKER_FORBIDDEN = (_LIB, "Input" + "Format", "Document" + "Stream")

# Excludes mirror the `| grep -v` filters on the PRD validation grep.
_EXCLUDED_DIRS = {".venv", "__pycache__", ".mypy_cache"}


def _backend_py_files() -> list[Path]:
    """Every tracked-style backend python file, pruning vendored/cache dirs so
    the scan matches `grep -r ... backend --include=*.py | grep -v .venv`."""
    files: list[Path] = []
    for path in BACKEND.rglob("*.py"):
        if _EXCLUDED_DIRS.isdisjoint(path.parts):
            files.append(path)
    return files


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_docling_confined_to_parsing_module() -> None:
    """Step 1: docling is imported in exactly one module — parsing.py."""
    offenders: dict[str, list[str]] = {}
    for path in _backend_py_files():
        src = path.read_text(encoding="utf-8")
        hits = [tok for tok in _IMPORT_TOKENS if tok in src]
        if hits:
            offenders[path.name] = hits
    _check(
        set(offenders) == {"parsing.py"},
        f"docling must be confined to parsing.py; found references elsewhere: {offenders}",
    )
    print("ok: docling import/types confined to backend/parsing.py")


def test_chunker_has_no_docling_reference() -> None:
    """Step 2: chunking.py references no docling type at all."""
    src = (BACKEND / "chunking.py").read_text(encoding="utf-8")
    hits = [tok for tok in _CHUNKER_FORBIDDEN if tok in src]
    _check(not hits, f"chunking.py must not reference docling; found {hits}")
    print("ok: backend/chunking.py has zero docling references")


def test_chunk_text_document_arg_is_str() -> None:
    """Step 3: chunk_text's document argument is typed `str` (the v1 contract)."""
    src = (BACKEND / "chunking.py").read_text(encoding="utf-8")
    fn = next(
        (
            n
            for n in ast.parse(src).body
            if isinstance(n, ast.FunctionDef) and n.name == "chunk_text"
        ),
        None,
    )
    _check(fn is not None, "chunk_text not found in backend/chunking.py")
    assert fn is not None  # narrow for type-checkers
    first = fn.args.args[0]
    _check(first.arg == "text", f"chunk_text's first arg should be `text`, got {first.arg!r}")
    _check(
        isinstance(first.annotation, ast.Name) and first.annotation.id == "str",
        "chunk_text's document argument must be annotated `str` (the boundary output contract)",
    )
    print("ok: chunk_text(text: str, ...) — boundary consumes only markdown str")


def main() -> int:
    tests = [
        test_docling_confined_to_parsing_module,
        test_chunker_has_no_docling_reference,
        test_chunk_text_document_arg_is_str,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} parsing-seam (US-036) checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
