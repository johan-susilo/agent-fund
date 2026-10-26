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


LLM_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "required": ["market_view", "inferences", "portfolio", "exits"],
    "properties": {
        "market_view": {
            "type": "OBJECT",
            "required": ["basis_refs", "logic", "regime", "stance", "net_exposure_intent", "target_cash_pct_range", "confidence"],
            "properties": {
                "basis_refs": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": 10, "description": "Max 10 IDs. Select only the top primary drivers. Internalize routine background data without citing it."},
                "logic": {"type": "STRING"},
                "regime": {"type": "STRING", "enum": ["risk_on", "neutral", "risk_off"]},
                "stance": {"type": "STRING", "enum": ["aggressive", "neutral", "defensive"]},
                "net_exposure_intent": {"type": "STRING", "enum": ["increase", "hold", "reduce"]},
                "target_cash_pct_range": {"type": "ARRAY", "items": {"type": "NUMBER"}},
                "counter_evidence": {"type": "STRING", "nullable": True},
                "confidence": {"type": "NUMBER"},
            },
        },
        "inferences": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "required": ["inf_id", "premise_refs", "logic", "conclusion", "confidence"],
                "properties": {
                    "inf_id": {"type": "STRING"},
                    "premise_refs": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "logic": {"type": "STRING"},
                    "conclusion": {"type": "STRING"},
                    "counter_evidence": {"type": "STRING", "nullable": True},
                    "confidence": {"type": "NUMBER"},
                },
            },
        },
        "portfolio": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "required": ["ticker", "target_weight", "inference_refs"],
                "properties": {
                    "ticker": {"type": "STRING"},
                    "target_weight": {"type": "NUMBER"},
                    "inference_refs": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        },
        "exits": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "required": ["ticker", "inference_refs"],
                "properties": {
                    "ticker": {"type": "STRING"},
                    "inference_refs": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        },
    },
}

def _config(system_prompt, model, temperature):
    from google.genai import types
    env_t = os.environ.get("LLM_TEMPERATURE")
    temp = float(env_t) if env_t else (0.1 if temperature is None else float(temperature))
    kw = dict(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=LLM_RESPONSE_SCHEMA,
        temperature=temp,
        seed=int(os.environ.get("LLM_SEED", "42")),
    )
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
    
    # Safely extract ONLY the JSON object, ignoring any <scratchpad> text before it
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in response")
        
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON Parsing Error: {e}")

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


def generate_raw(contents, config):
    """One generate_content call with retry/model fallback, for multi-turn tool loops."""
    global _working_model
    from google.genai import errors
    for model in candidates():
        for attempt in range(MAX_RETRIES):
            try:
                resp = get_client().models.generate_content(model=model, contents=contents, config=config)
                _working_model = model
                return resp, model
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code == 404:
                    break
                if code in RETRY_CODES and attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt + random.random())
                    continue
                raise
    raise RuntimeError("no usable model for tool loop; check GOOGLE_CLOUD_LOCATION / GEMINI_MODEL")


# ---- thread-safe client (added by the technical_agent update) ----------------------------------
# One shared genai client used from several worker threads produced "client has been closed" and SSL
# decryption errors in the macro / fundamental research agents. One client per thread avoids it.
import threading as _threading
_tls = _threading.local()


def get_client():
    c = getattr(_tls, "client", None)
    if c is None:
        from google import genai
        c = genai.Client(vertexai=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                         location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
        _tls.client = c
    return c


if __name__ == "__main__":       # connectivity test
    print(ask_json_meta("Reply only with JSON.", 'Return {"ok": true}')[0])
