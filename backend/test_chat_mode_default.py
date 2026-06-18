"""US-025 validation test: Responses-mode fail-closed default-chat-mode binding.

Exercises `model_config.resolve_chat_mode_default` directly — the pure startup
validator that `main.py` calls once against the resolved answerer config. The
function reads no environment itself (the caller passes the raw
`CHAT_MODE_DEFAULT` value), so this test constructs `ProviderConfig`s inline and
needs no DB / network / secrets — it runs anywhere, like `test_model_config.py`.

Covers the PRD validation test (assert-style fail-closed guard):
  * answerer provider=azure + CHAT_MODE_DEFAULT=responses -> RuntimeError at
    startup, message naming the provider AND the `completions` remedy (no silent
    fallback);
  * answerer provider=azure with CHAT_MODE_DEFAULT unset -> resolves to
    `completions` (the portable cross-provider default);
  * answerer provider=openai + responses -> still resolves to `responses`
    (the OpenAI-only enhancement stays available);
plus the surrounding matrix (openai default preserved, explicit completions
always honored, bad value -> ValueError).

Run:
    python -m backend.test_chat_mode_default
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from model_config import ProviderConfig, resolve_chat_mode_default  # noqa: E402

# Minimal valid configs per provider. resolve_chat_mode_default only reads
# `.provider`, but we build real (frozen) configs so the test exercises the
# actual type the startup path passes in.
_OPENAI = ProviderConfig(provider="openai", api_key="sk-test")
_AZURE = ProviderConfig(
    provider="azure",
    api_key="az-key",
    azure_endpoint="https://contoso.openai.azure.com",
    api_version="2024-10-21",
)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_azure_plus_responses_fails_closed() -> None:
    """PRD core case: a non-openai answerer with CHAT_MODE_DEFAULT=responses
    refuses to start (RuntimeError) — never a silent downgrade to completions.
    The message must name the offending provider AND the completions remedy."""
    try:
        resolve_chat_mode_default(_AZURE, "responses")
    except RuntimeError as e:
        text = str(e)
        _check("azure" in text, f"error must name the offending provider, got: {text!r}")
        _check(
            "completions" in text,
            f"error must carry the CHAT_MODE_DEFAULT=completions remedy, got: {text!r}",
        )
        _check(
            "provider=openai" in text,
            f"error must offer the use-openai remedy, got: {text!r}",
        )
        print("ok: azure + responses fails closed (RuntimeError) with provider + remedy")
        return
    raise AssertionError("azure + responses must raise RuntimeError, not fall back")


def test_azure_unset_defaults_to_completions() -> None:
    """A non-openai answerer with CHAT_MODE_DEFAULT unset resolves to the
    portable `completions` path — the cross-provider default flip."""
    for raw in (None, "", "   "):
        mode = resolve_chat_mode_default(_AZURE, raw)
        _check(
            mode == "completions",
            f"azure with raw={raw!r} should default to completions, got {mode!r}",
        )
    print("ok: azure with no explicit mode resolves to completions (default flip)")


def test_azure_explicit_completions_is_honored() -> None:
    """`completions` is portable, so it is always accepted under any provider."""
    mode = resolve_chat_mode_default(_AZURE, "completions")
    _check(mode == "completions", f"azure + completions should resolve, got {mode!r}")
    print("ok: azure + explicit completions is honored")


def test_openai_responses_still_starts() -> None:
    """The control: openai + responses is the one valid Responses combination
    and must still resolve (the OpenAI-only enhancement stays available)."""
    mode = resolve_chat_mode_default(_OPENAI, "responses")
    _check(mode == "responses", f"openai + responses should resolve, got {mode!r}")
    print("ok: openai + responses still resolves (OpenAI-only enhancement intact)")


def test_openai_unset_preserves_responses_default() -> None:
    """For an openai answerer, the historical US-004 `responses` default is
    preserved when CHAT_MODE_DEFAULT is unset (only the cross-provider flips)."""
    for raw in (None, "", "  "):
        mode = resolve_chat_mode_default(_OPENAI, raw)
        _check(
            mode == "responses",
            f"openai with raw={raw!r} should keep the responses default, got {mode!r}",
        )
    print("ok: openai with no explicit mode keeps the historical responses default")


def test_openai_explicit_completions() -> None:
    """An openai operator may still opt into the portable path explicitly."""
    mode = resolve_chat_mode_default(_OPENAI, "completions")
    _check(mode == "completions", f"openai + completions should resolve, got {mode!r}")
    print("ok: openai + explicit completions is honored")


def test_case_and_whitespace_insensitive() -> None:
    """Raw env values are normalized (trimmed + lowercased), matching the prior
    CHAT_MODE_DEFAULT parsing — so ` RESPONSES ` on azure still fails closed."""
    try:
        resolve_chat_mode_default(_AZURE, "  RESPONSES  ")
    except RuntimeError:
        print("ok: mixed-case/padded 'responses' is normalized before the guard")
        return
    raise AssertionError("normalized 'responses' on azure must still fail closed")


def test_invalid_value_fails_closed() -> None:
    """A typo in CHAT_MODE_DEFAULT raises ValueError (never silently ignored),
    independent of provider — same spirit as the provider-string validation."""
    for cfg in (_OPENAI, _AZURE):
        try:
            resolve_chat_mode_default(cfg, "responsez")
        except ValueError:
            continue
        raise AssertionError(
            f"invalid CHAT_MODE_DEFAULT must raise ValueError (provider={cfg.provider})"
        )
    print("ok: an invalid CHAT_MODE_DEFAULT fails closed (ValueError) on every provider")


def main() -> int:
    tests = [
        test_azure_plus_responses_fails_closed,
        test_azure_unset_defaults_to_completions,
        test_azure_explicit_completions_is_honored,
        test_openai_responses_still_starts,
        test_openai_unset_preserves_responses_default,
        test_openai_explicit_completions,
        test_case_and_whitespace_insensitive,
        test_invalid_value_fails_closed,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} chat_mode_default test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
