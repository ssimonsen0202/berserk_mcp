#!/usr/bin/env python3
"""Provider-change fingerprints for model-behavior monitoring (issue #90).

Two independent signals, because either alone has a blind spot:

  metadata_fingerprint   -- hashes the provider's own /models entry for one
                            model (context length, pricing, version fields).
                            Catches a declared change.
  behavioral_fingerprint -- hashes temperature-0 completions for a fixed
                            prompt set. Catches a silent weights swap that
                            left /models metadata untouched.

IMPORTANT: a changed behavioral fingerprint is a SIGNAL TO INVESTIGATE, not
proof the provider swapped the model. Temperature 0 is not guaranteed
deterministic across providers -- batching and hardware nondeterminism can
change output with no model change. Every caller must report it that way.
"""
import hashlib
import json
import sys
from pathlib import Path

# Add the repo root to path so we can import _http and parser_factory
_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))
import _http  # noqa: E402
import parser_factory as pf  # noqa: E402

# Deliberately short, deterministic, and cheap. These are fingerprint probes,
# not quality tests -- quality is measured by the scored canary.
FINGERPRINT_PROMPTS = (
    "Reply with exactly the word: ready",
    "Return only the number 42.",
    "Answer with one word: what colour is a clear midday sky?",
)


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _normalize(value):
    """Order-independent, whitespace-insensitive canonical form."""
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def metadata_fingerprint(models_payload, model_id):
    """Hash one model's /models entry. Returns None if the model is absent --
    which is itself worth reporting, since a model vanishing from the catalog
    is a provider change."""
    entries = models_payload.get("data") or []
    for entry in entries:
        if entry.get("id") == model_id:
            canonical = json.dumps(_normalize(entry), sort_keys=True, separators=(",", ":"))
            return _hash(canonical)
    return None


def behavioral_fingerprint(completions):
    """Hash normalized completions for the fixed prompt set."""
    canonical = json.dumps([_normalize(c) for c in completions],
                           sort_keys=True, separators=(",", ":"))
    return _hash(canonical)


def fetch_models(chat_completions_url, api_key):
    """Fetch the provider's /models endpoint and return the parsed payload.

    Derives the /models endpoint from a chat-completions URL using
    parser_factory.hermes_models_url(), then fetches through the shared
    HTTP policy layer (_http) so scheme allowlisting, redirect refusal,
    and response-size caps all apply.

    Returns (payload_dict, None) on success, or (None, error_string) on failure.
    """
    models_url = pf.hermes_models_url(chat_completions_url)
    if not models_url:
        return None, f"cannot derive /models endpoint from {chat_completions_url!r}"

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload, err = _http.http_get_json(models_url, headers)
    if err:
        return None, f"failed to fetch models from {models_url!r}: {err}"

    return payload, None
