"""LLM Skill Factory — converts SKILL.md body into an executable skill function.

The SKILL.md body is captured in a closure and used as the LLM system prompt.
It NEVER leaves the provider's machine. Callers only see input/output.

This is the core of Skill-as-API: the skill runs on your machine,
powered by your LLM API key, using your private instructions.
"""

import json
import logging
import os
import time
import threading
import urllib.request
from typing import Dict, Optional, Tuple

logger = logging.getLogger("coworker.llm_skill")

# Supported LLM providers
PROVIDERS = {
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
    },
}


def detect_provider() -> Tuple[str, str]:
    """Auto-detect available LLM provider from environment variables.

    Returns:
        (provider_name, api_key) or ("", "") if none found.
    """
    for name, cfg in PROVIDERS.items():
        key = os.getenv(cfg["env_key"], "")
        if key:
            return name, key
    return "", ""


class _RateLimiter:
    """Simple per-minute rate limiter."""

    def __init__(self, max_calls_per_minute: int = 20):
        self.max_rpm = max_calls_per_minute
        self._timestamps: list = []
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60]
            if len(self._timestamps) >= self.max_rpm:
                wait = 60 - (now - self._timestamps[0])
                raise RuntimeError(
                    f"Rate limit: {self.max_rpm} calls/min exceeded. "
                    f"Try again in {wait:.0f}s"
                )
            self._timestamps.append(now)


def _call_llm(
    provider: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_input: str,
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> str:
    """Call LLM API. Returns the assistant's response text."""
    data = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()

    url = PROVIDERS.get(provider, PROVIDERS["deepseek"])["url"]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("LLM call failed (%s/%s): %s", provider, model, e)
        raise RuntimeError(f"LLM error: {str(e)[:200]}")


def _call_llm_with_retry(
    provider: str, api_key: str, model: str,
    system_prompt: str, user_input: str,
    temperature: float = 0.7, max_tokens: int = 2000,
    retries: int = 2, backoff: float = 1.0,
) -> str:
    """Call LLM with retry on failure."""
    for attempt in range(retries + 1):
        try:
            return _call_llm(
                provider, api_key, model,
                system_prompt, user_input,
                temperature, max_tokens,
            )
        except RuntimeError:
            if attempt == retries:
                raise
            time.sleep(backoff * (attempt + 1))
    return ""  # unreachable


def make_llm_skill_func(
    system_prompt: str,
    provider: str,
    api_key: str,
    model: str,
    temperature: float = 0.7,
    max_tokens: int = 2000,
    input_schema: dict = None,
    rate_limit: int = 20,
):
    """Create a closure function that executes a SKILL.md via LLM.

    The system_prompt (SKILL.md body) is captured in the closure
    and NEVER transmitted to callers. This is the core of Skill-as-API.

    Args:
        system_prompt: The SKILL.md body — private instructions.
        provider: LLM provider name (deepseek/openai).
        api_key: Provider's API key (from env var).
        model: Model name.
        temperature: LLM temperature.
        max_tokens: Max output tokens.
        input_schema: Expected input parameters.
        rate_limit: Max calls per minute.

    Returns:
        A function(kwargs) -> dict that can be registered as a CoWorker skill.
    """
    schema = input_schema or {"input": "str"}
    limiter = _RateLimiter(rate_limit)

    def _skill_func(**kwargs) -> dict:
        # Rate limit
        limiter.acquire()

        # Build user message from kwargs
        if len(schema) == 1 and "input" in schema:
            user_msg = str(kwargs.get("input", ""))
        else:
            parts = []
            for k, v in kwargs.items():
                parts.append(f"{k}: {v}")
            user_msg = "\n".join(parts)

        if not user_msg.strip():
            return {"result": "[error: empty input]"}

        result = _call_llm_with_retry(
            provider=provider,
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_input=user_msg,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return {"result": result}

    return _skill_func
