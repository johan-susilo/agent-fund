"""Vertex AI (Google Gen AI SDK) connection layer: lazy client, retry, model fallback.

.env keys:
    GOOGLE_CLOUD_PROJECT=<project id>
    GOOGLE_CLOUD_LOCATION=global              # Gemini 3.x normally needs "global"
    GEMINI_MODEL=gemini-3.8-flash             # primary model
    GEMINI_FALLBACK_MODELS=a,b                # optional, tried in order on HTTP 404
    LLM_TEMPERATURE=0.2                       # optional; not sent for Gemini 3.x unless set
"""
from __future__ import annotations

import json
import os
import random
import re
import time

from dotenv import load_dotenv   # pip install python-dotenv

load_dotenv()

DEFAULT_FALLBACKS = "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
RETRY_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4

_client = None
_working_model = None            # model verified earlier in this process


def get_client():
    """Create the client on first use so offline tests and --replay never need credentials."""
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(vertexai=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                               location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
    return _client


def candidates() -> list:
    names = [os.environ.get("GEMINI_MODEL", "").strip()]
    names += [m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS).split(",")]
    names = [n for n in dict.fromkeys(names) if n]
    if _working_model in names:                       # try the last known-good model first
        names.remove(_working_model)
        names.insert(0, _working_model)
    return names


def _config(system_prompt, model, temperature):
    from google.genai import types
    kw = dict(system_instruction=system_prompt, response_mime_type="application/json")
    env_t = os.environ.get("LLM_TEMPERATURE")
    if temperature is None:
        if not model.startswith("gemini-3"):
            temperature = float(env_t or 0.2)
        elif env_t:
            temperature = float(env_t)
    if temperature is not None:
        kw["temperature"] = temperature
    return types.GenerateContentConfig(**kw)


def _generate(model, system_prompt, user_content, temperature):
    """One model call with exponential backoff on transient errors."""
    from google.genai import errors
    for attempt in range(MAX_RETRIES):
        try:
            return get_client().models.generate_content(
                model=model, contents=user_content, config=_config(system_prompt, model, temperature))
        except errors.APIError as e:
            if getattr(e, "code", None) in RETRY_CODES and attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt + random.random()
                print(f"[llm] {model} error {e.code}, retrying in {wait:.1f}s ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise


def _parse(text):
    if not text:
        raise ValueError("empty model response (possibly blocked by safety filters)")
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))


def ask_json_meta(system_prompt: str, user_content: str, temperature=None):
    """Return (dict, meta, raw_text). meta['model'] is the model actually used (written to the D-Plan)."""
    global _working_model
    from google.genai import errors
    tried = []
    for model in candidates():
        try:
            resp = _generate(model, system_prompt, user_content, temperature)
        except errors.APIError as e:
            if getattr(e, "code", None) == 404:       # retired / unavailable in this region -> next model
                tried.append(model)
                print(f"[llm] {model} returned 404, trying the next model")
                continue
            raise                                      # auth / argument errors: retrying will not help
        _working_model = model
        u = resp.usage_metadata
        meta = {"model": model,
                "input_tokens": int(getattr(u, "prompt_token_count", 0) or 0),
                # thinking tokens are billed as output, so include them
                "output_tokens": int((getattr(u, "candidates_token_count", 0) or 0)
                                     + (getattr(u, "thoughts_token_count", 0) or 0))}
        return _parse(resp.text), meta, resp.text
    raise RuntimeError(f"no usable model among {tried}; check GOOGLE_CLOUD_LOCATION (Gemini 3.x needs 'global')")


if __name__ == "__main__":       # connectivity test
    print(ask_json_meta("Reply only with JSON.", 'Return {"ok": true}')[0])
