"""Vertex AI (Google Gen AI SDK) 連線層：固定、可重試、可降級。

.env 需要:
    GOOGLE_CLOUD_PROJECT=你的專案ID
    GOOGLE_CLOUD_LOCATION=global            # Gemini 3.x 建議用 global
    GEMINI_MODEL=gemini-3.8-flash           # 主要模型
    GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite   # 選填：主模型 404 時依序嘗試
    LLM_TEMPERATURE=0.2                     # 選填；Gemini 3.x 預設不傳（Google 建議維持預設 1.0）
先跑 `python src/check_vertex.py` 確認哪個「地區 × 模型」組合在你的專案可用。
"""
import json
import os
import random
import re
import time

from dotenv import load_dotenv   # pip install python-dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
DEFAULT_FALLBACKS = "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
RETRY_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4

client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
_working_model = None            # 同一次執行內，記住已驗證可用的模型


def candidates():
    main = os.environ.get("GEMINI_MODEL", "").strip()
    fb = os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS).split(",")
    names = [main] + [m.strip() for m in fb]
    return [n for n in dict.fromkeys(names) if n]      # 去重並保持順序


def _config(system_prompt, model, temperature):
    kw = dict(system_instruction=system_prompt, response_mime_type="application/json")
    if temperature is None and not model.startswith("gemini-3"):
        temperature = float(os.environ.get("LLM_TEMPERATURE", 0.2))
    elif temperature is None and os.environ.get("LLM_TEMPERATURE"):
        temperature = float(os.environ["LLM_TEMPERATURE"])
    if temperature is not None:
        kw["temperature"] = temperature
    return types.GenerateContentConfig(**kw)


def _parse(text):
    if not text:
        raise ValueError("模型回傳空內容（可能被安全機制擋下）")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def ask_json_meta(system_prompt: str, user_content: str, temperature=None):
    """回傳 (dict, meta, 原始文字)。meta['model'] 是『實際使用』的模型，會寫進 D-Plan。"""
    global _working_model
    order = [_working_model] if _working_model else candidates()
    tried = []
    for model in order:
        for attempt in range(MAX_RETRIES):
            try:
                resp = client.models.generate_content(
                    model=model, contents=user_content,
                    config=_config(system_prompt, model, temperature))
                break
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code == 404:                       # 模型不存在/已退役/此地區沒有 -> 換下一個
                    tried.append(f"{model}: 404")
                    print(f"[llm] {model} 404，改試下一個模型")
                    resp = None
                    break
                if code in RETRY_CODES and attempt < MAX_RETRIES - 1:
                    wait = 2 ** attempt + random.random()
                    print(f"[llm] {model} 錯誤 {code}，{wait:.1f}s 後重試 ({attempt + 1}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                raise                                  # 權限/參數錯誤：重試沒用，直接丟出
        else:
            resp = None
        if resp is None:
            continue
        _working_model = model
        u = resp.usage_metadata
        meta = {"model": model,
                "input_tokens": int(getattr(u, "prompt_token_count", 0) or 0),
                "output_tokens": int(getattr(u, "candidates_token_count", 0) or 0)}
        return _parse(resp.text), meta, resp.text
    raise RuntimeError(
        f"所有模型都不可用 {tried}（project={PROJECT}, location={LOCATION}）。"
        "請執行 python src/check_vertex.py；Gemini 3.x 多半需要 GOOGLE_CLOUD_LOCATION=global")


def ask_json(system_prompt: str, user_content: str) -> dict:
    return ask_json_meta(system_prompt, user_content)[0]


if __name__ == "__main__":       # 連線測試
    print(ask_json("Reply only with JSON.", 'Return {"ok": true}'))