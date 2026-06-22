"""US-037 validation test: the `DocumentParser` boundary contract.

Proves the ingestion parser seam (ADR-0007) is now an explicit, typed
extension point — a one-method ABC modeled on `Reranker` / `WebSearchProvider`:

  1. `DocumentParser` is an `abc.ABC` and cannot be instantiated directly
     (its `parse` is abstract) — it is a contract, not a usable object.
  2. `parse`'s signature matches today's `parse_document` exactly
     (`bytes`, `str`, `str | None`) -> `str`, the pinned v1 contract.
  3. The shape mirrors the existing pattern: a class-level `name` plus exactly
     one abstractmethod, so a reader who knows `Reranker` recognizes it.
  4. A minimal concrete subclass implementing `parse` instantiates and returns
     a `str` — proving the slot is pluggable (the path US-038/US-043 take).

Run:
    python -m backend.test_parsing_protocol
"""

from __future__ import annotations

import abc
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from parsing import DocumentParser, parse_document  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_is_abstract_and_not_instantiable() -> None:
    """Step 1: DocumentParser is an abc.ABC whose `parse` is abstract, so
    instantiating it directly raises TypeError."""
    _check(issubclass(DocumentParser, abc.ABC), "DocumentParser must subclass abc.ABC")
    _check(
        DocumentParser.__abstractmethods__ == frozenset({"parse"}),
        f"parse must be the sole abstractmethod, got {set(DocumentParser.__abstractmethods__)}",
    )
    try:
        DocumentParser()  # type: ignore[abstract]
    except TypeError:
        pass
    else:
        raise AssertionError("instantiating DocumentParser() directly must raise TypeError")
    print("ok: DocumentParser is abstract — direct instantiation raises TypeError")


def test_parse_signature_matches_parse_document() -> None:
    """Step 2: parse's signature == today's parse_document, sans `self`."""
    proto = inspect.signature(DocumentParser.parse)
    params = list(proto.parameters.values())
    _check(params and params[0].name == "self", "parse must be an instance method (self first)")
    proto_no_self = proto.replace(parameters=params[1:])

    fn = inspect.signature(parse_document)
    _check(
        proto_no_self == fn,
        f"parse signature {proto_no_self} must match parse_document {fn} exactly",
    )

    # And it is literally (bytes, filename, content_type: str | None) -> str.
    rest = params[1:]
    names = [p.name for p in rest]
    annos = [p.annotation for p in rest]
    _check(names == ["raw", "filename", "content_type"], f"unexpected params: {names}")
    _check(
        annos == ["bytes", "str", "str | None"],
        f"contract must be (bytes, str, str | None), got {annos}",
    )
    _check(proto.return_annotation == "str", f"parse must return str, got {proto.return_annotation!r}")
    print(f"ok: parse{proto_no_self} matches parse_document — (bytes, str, str | None) -> str")


def test_shape_mirrors_existing_pattern() -> None:
    """Step 3: class-level `name: str` + exactly one abstractmethod, like
    Reranker / WebSearchProvider."""
    _check(
        DocumentParser.__annotations__.get("name") == "str",
        "DocumentParser must declare a class-level `name: str`",
    )
    abstracts = DocumentParser.__abstractmethods__
    _check(len(abstracts) == 1, f"exactly one abstractmethod expected, got {set(abstracts)}")
    print("ok: shape mirrors Reranker/WebSearchProvider — name: str + one abstractmethod")


def test_concrete_subclass_is_pluggable() -> None:
    """Step 4: a minimal concrete subclass instantiates and returns a str."""

    class _EchoParser(DocumentParser):
        name = "echo"

        def parse(self, raw: bytes, filename: str, content_type: str | None = None) -> str:
            return raw.decode("utf-8")

    parser = _EchoParser()  # must NOT raise — parse is implemented
    out = parser.parse(b"# hi\n\nbody", "x.md", "text/markdown")
    _check(isinstance(out, str), "a concrete parser's parse must return str")
    _check(out == "# hi\n\nbody", f"echo parser should round-trip bytes, got {out!r}")
    print("ok: a concrete DocumentParser subclass instantiates and returns markdown str")


def main() -> int:
    tests = [
        test_is_abstract_and_not_instantiable,
        test_parse_signature_matches_parse_document,
        test_shape_mirrors_existing_pattern,
        test_concrete_subclass_is_pluggable,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} DocumentParser protocol (US-037) checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
