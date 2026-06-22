"""US-039 validation test: `PARSER` env selection + `build_parser` factory.

Proves swapping the ingestion parser is a config switch that fails loudly and
*at build time*, mirroring `get_reranker_name` / `build_reranker`:

  1. `PARSER` unset → `build_parser(get_parser_name())` is a `DoclingParser`
     (default preserves today's behavior).
  2. `PARSER=bogus` → `get_parser_name()` raises `ValueError` listing the
     valid options (a typo never silently falls back to docling).
  3. `PARSER=llamaparse` with no `LLAMA_CLOUD_API_KEY` → `build_parser` raises
     `ValueError` naming the missing key, at build time (no network call).
  4. `PARSER=llamaparse` *with* a key, and `PARSER=unstructured`, fail loudly —
     they never silently fall back to docling.
  5. `get_selected_parser()` builds once / caches (the startup fail-closed
     hook), and `reset_selected_parser()` drops the cache.

Run:
    python -m backend.test_parser_selection
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from parsing import (  # noqa: E402
    DoclingParser,
    DocumentParser,
    build_parser,
    get_parser_name,
    get_selected_parser,
    reset_selected_parser,
)

_MANAGED_KEYS = ("PARSER", "LLAMA_CLOUD_API_KEY")


@contextmanager
def _env(**overrides: str) -> Iterator[None]:
    """Run a case with exactly `overrides` set for the managed keys, restoring
    the prior environment (and the cached parser) afterward."""
    saved = {k: os.environ.get(k) for k in _MANAGED_KEYS}
    try:
        for k in _MANAGED_KEYS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[k] = v
        reset_selected_parser()
        yield
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        reset_selected_parser()


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_default_is_docling() -> None:
    """PARSER unset → docling, behavior unchanged."""
    with _env():
        _check(get_parser_name() == "docling", "PARSER unset must default to 'docling'")
        parser = build_parser(get_parser_name())
        _check(isinstance(parser, DoclingParser), "default parser must be DoclingParser")
        _check(parser.name == "docling", f"name must be 'docling', got {parser.name!r}")
    print("ok: PARSER unset -> DoclingParser (default preserved)")


def test_unknown_value_fails_fast() -> None:
    """PARSER=bogus → ValueError listing the valid options (no docling fallback)."""
    with _env(PARSER="bogus"):
        try:
            get_parser_name()
        except ValueError as e:
            msg = str(e)
            _check(
                all(opt in msg for opt in ("docling", "unstructured", "llamaparse")),
                f"error must list valid options, got: {msg}",
            )
        else:
            raise AssertionError("PARSER=bogus must raise ValueError, not default to docling")
    print("ok: PARSER=bogus -> ValueError listing docling|unstructured|llamaparse")


def test_llamaparse_missing_key_fails_at_build() -> None:
    """PARSER=llamaparse with no key → ValueError naming the key, at build time."""
    with _env(PARSER="llamaparse"):
        name = get_parser_name()
        _check(name == "llamaparse", "llamaparse must be an accepted name")
        try:
            build_parser(name)
        except ValueError as e:
            _check(
                "LLAMA_CLOUD_API_KEY" in str(e),
                f"missing-key error must name LLAMA_CLOUD_API_KEY, got: {e}",
            )
        else:
            raise AssertionError("PARSER=llamaparse without a key must raise at build time")
    print("ok: PARSER=llamaparse without key -> build-time ValueError naming LLAMA_CLOUD_API_KEY")


def test_llamaparse_with_key_never_falls_back_to_docling() -> None:
    """A *keyed* llamaparse selection must NOT silently become docling.

    The working adapter ships in US-040; until then build raises. The contract
    under test is only 'never silently docling'."""
    with _env(PARSER="llamaparse", LLAMA_CLOUD_API_KEY="test-key"):
        try:
            result = build_parser(get_parser_name())
        except Exception as e:  # noqa: BLE001 — any loud failure is acceptable here
            _check(
                not isinstance(e, ValueError) or "LLAMA_CLOUD_API_KEY" not in str(e),
                "with a key present the missing-key error must not fire",
            )
        else:
            _check(
                not isinstance(result, DoclingParser),
                "PARSER=llamaparse must never silently resolve to DoclingParser",
            )
    print("ok: PARSER=llamaparse (keyed) never silently falls back to docling")


def test_unstructured_is_accepted_but_fails_loud() -> None:
    """unstructured is a named/accepted BYO slot — build fails loudly, never docling."""
    with _env(PARSER="unstructured"):
        _check(get_parser_name() == "unstructured", "unstructured must be an accepted name")
        try:
            result = build_parser(get_parser_name())
        except Exception:  # noqa: BLE001 — loud failure is the requirement
            pass
        else:
            raise AssertionError(
                f"PARSER=unstructured must fail loudly, not return {type(result).__name__}"
            )
    print("ok: PARSER=unstructured is accepted but build fails loudly (no docling fallback)")


def test_all_valid_names_and_normalization() -> None:
    """Each valid value resolves; case/whitespace is normalized."""
    for raw, expected in (
        ("docling", "docling"),
        ("unstructured", "unstructured"),
        ("llamaparse", "llamaparse"),
        ("  DOCLING  ", "docling"),
        ("LlamaParse", "llamaparse"),
    ):
        with _env(PARSER=raw):
            got = get_parser_name()
            _check(got == expected, f"PARSER={raw!r} should resolve to {expected!r}, got {got!r}")
    print("ok: all valid PARSER values resolve; case/whitespace normalized")


def test_selected_parser_cached_and_resettable() -> None:
    """get_selected_parser builds once (the startup fail-closed hook) and caches."""
    with _env():
        first = get_selected_parser()
        second = get_selected_parser()
        _check(isinstance(first, DocumentParser), "get_selected_parser must return a DocumentParser")
        _check(first is second, "get_selected_parser must cache a single instance")
        reset_selected_parser()
        third = get_selected_parser()
        _check(third is not first, "reset_selected_parser must drop the cached instance")
        _check(isinstance(third, DoclingParser), "default selected parser must be docling")
    print("ok: get_selected_parser caches once and reset_selected_parser clears it")


def main() -> int:
    tests = [
        test_default_is_docling,
        test_unknown_value_fails_fast,
        test_llamaparse_missing_key_fails_at_build,
        test_llamaparse_with_key_never_falls_back_to_docling,
        test_unstructured_is_accepted_but_fails_loud,
        test_all_valid_names_and_normalization,
        test_selected_parser_cached_and_resettable,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} parser-selection (US-039) checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
