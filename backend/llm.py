"""llm.py - minimal Gemini client over plain HTTPS (requests). No SDK, no tools, text/JSON in and out.

* API key is read from .env and sent in the x-goog-api-key header (never in a URL, never logged).
* Explicit timeout + retries on transient errors (429 / 5xx), then fallback model.
* JSON mode: Gemini is forced to answer with JSON matching a schema; we still parse/validate it ourselves.
"""

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Tried in order. Chosen on 2026-11-16 by measuring which models answered JSON-mode calls in ~4s
# (gemini-2.5-flash is closed to new keys; the larger flash models were returning 503 "high demand").
MODEL_CHAIN = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]
TIMEOUT = 20             # per HTTP call
DEADLINE = 45            # whole generate() call, across retries and fallback models
RETRIES = 1
RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """Safe-to-show error (never contains the API key)."""


def _api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or key == "your-key-here":
        raise LLMError("GEMINI_API_KEY is not set in .env")
    return key


def is_configured():
    try:
        _api_key()
        return True
    except LLMError:
        return False


def _call(model, body, deadline):
    url = f"{API_BASE}/{model}:generateContent"
    headers = {"x-goog-api-key": _api_key(), "Content-Type": "application/json"}
    last = "unknown error"
    for attempt in range(RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining < 2:
            raise LLMError("AI time budget used up")
        try:
            r = requests.post(url, headers=headers, json=body, timeout=min(TIMEOUT, remaining))
        except requests.Timeout:
            last = f"Gemini did not respond within {TIMEOUT}s"
        except requests.RequestException as e:
            last = f"could not reach Gemini ({type(e).__name__})"
        else:
            if r.ok:
                return r.json()
            try:
                msg = r.json().get("error", {}).get("message", "")[:200]
            except ValueError:
                msg = ""
            last = f"Gemini HTTP {r.status_code}: {msg[:90]}"
            if r.status_code not in RETRY_STATUS:
                break
        if attempt < RETRIES:
            time.sleep(1.0)
    raise LLMError(last)


def model_chain():
    custom = os.environ.get("GEMINI_MODEL", "").strip()
    return list(dict.fromkeys(([custom] if custom else []) + MODEL_CHAIN))


def generate(system, prompt, schema=None, temperature=0.2):
    """Return (result, model_used). result is text, or a parsed JSON dict when a schema is given."""
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = schema

    deadline = time.monotonic() + DEADLINE
    errors = []
    for model in model_chain():
        try:
            data = _call(model, body, deadline)
        except LLMError as e:
            errors.append(f"{model}: {e}")
            if time.monotonic() > deadline - 2:
                break
            continue
        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "no candidates")
            errors.append(f"{model}: empty response ({reason})")
            continue
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            errors.append(f"{model}: empty text (finishReason={candidates[0].get('finishReason')})")
            continue
        if not schema:
            return text, model
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError
            return parsed, model
        except ValueError:
            errors.append(f"{model}: response was not valid JSON")
    raise LLMError("; ".join(errors))
