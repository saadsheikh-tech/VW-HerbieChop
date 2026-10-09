"""Thin, provider-neutral LLM wrapper. Optional by design.

Every call site has a deterministic fallback, so the hub runs (and all tests pass)
without credentials. That mirrors the in-car split: rule-based edge logic always
works, cloud reasoning adds quality when the connection is there.

Providers (HERBIE_LLM_PROVIDER, or picked from whichever key is set):
  gemini     GEMINI_API_KEY      default model gemini-3.5-flash-lite (~0.7 s with minimal thinking)
  anthropic  ANTHROPIC_API_KEY   default model claude-opus-5-5
HERBIE_LLM=off forces rule-based mode; HERBIE_MODEL overrides the model.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("herbie.llm")

DEFAULT_MODELS = {"gemini": "gemini-3.5-flash-lite", "anthropic": "claude-opus-5-5"}
# Our effort scale -> Gemini thinking level. Spoken replies need "low" to stay under ~1 s.
GEMINI_THINKING = {"low": "minimal", "medium": "low", "high": "high"}


def _pick_provider() -> str | None:
    explicit = os.environ.get("HERBIE_LLM_PROVIDER", "").lower()
    if explicit in DEFAULT_MODELS:
        return explicit
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    return None


class LLM:
    def __init__(self) -> None:
        flag = os.environ.get("HERBIE_LLM", "").lower()
        self.provider = None if flag == "off" else _pick_provider()
        self.model = os.environ.get("HERBIE_MODEL") or DEFAULT_MODELS.get(self.provider or "", "none")
        self.effort = os.environ.get("HERBIE_EFFORT", "low")
        self.enabled = self.provider is not None
        self._client = None
        self.last_error: str | None = None

    def available(self) -> bool:
        return self.enabled

    def complete(self, system: str, user: str, schema: dict | None = None, effort: str | None = None,
                 max_tokens: int = 2000) -> str | dict | None:
        """Return text (or parsed JSON when `schema` is given), or None on any failure."""
        if not self.enabled:
            return None
        effort = effort or self.effort
        call = self._gemini if self.provider == "gemini" else self._anthropic
        text = call(system, user, schema, effort, max_tokens)
        if text is None:
            return None
        if schema:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                self.last_error = "invalid JSON"
                return None
        self.last_error = None
        return text.strip()

    def vision(self, system: str, prompt: str, jpeg: bytes) -> str | None:
        """One image + question → short text answer. Used only when the driver asks."""
        if not self.enabled:
            return None
        try:
            if self.provider == "gemini":
                from google import genai
                from google.genai import types

                if self._client is None:
                    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                    self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15_000))
                r = self._client.models.generate_content(
                    model=self.model,
                    contents=[types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), prompt],
                    config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=300,
                                                       thinking_config=types.ThinkingConfig(thinking_level="minimal")))
                return (r.text or "").strip() or None
            import base64

            import anthropic

            if self._client is None:
                self._client = anthropic.Anthropic(timeout=30.0, max_retries=1)
            response = self._client.messages.create(
                model=self.model, max_tokens=1000, system=system, output_config={"effort": "low"},
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(jpeg).decode()}},
                    {"type": "text", "text": prompt}]}])
            if response.stop_reason == "refusal":
                return None
            return next((b.text for b in response.content if b.type == "text"), None)
        except Exception as e:  # vision is a nice-to-have; never break the dialogue
            self.last_error = f"vision: {type(e).__name__}"
            log.warning("vision call failed: %s", e)
            return None

    # ---- Gemini ------------------------------------------------------------
    def _gemini(self, system, user, schema, effort, max_tokens) -> str | None:
        from google import genai
        from google.genai import errors, types

        if self._client is None:
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15_000))
        cfg = dict(system_instruction=system, max_output_tokens=max_tokens,
                   thinking_config=types.ThinkingConfig(thinking_level=GEMINI_THINKING.get(effort, "minimal")))
        if schema:
            cfg.update(response_mime_type="application/json", response_json_schema=schema)
        try:
            r = self._client.models.generate_content(model=self.model, contents=user,
                                                     config=types.GenerateContentConfig(**cfg))
        except errors.ClientError as e:  # 4xx: bad key, quota, bad request
            self.last_error = f"Gemini {e.code}"
            if e.code in (401, 403):
                self.enabled = False
            log.warning("Gemini client error %s: %s", e.code, str(e)[:200])
            return None
        except errors.ServerError as e:  # 5xx / overloaded: try again next time
            self.last_error = f"Gemini {e.code}"
            return None
        except Exception as e:  # network/timeouts
            self.last_error = type(e).__name__
            return None
        if not r.text:
            self.last_error = "empty or blocked response"
            return None
        return r.text

    # ---- Anthropic ---------------------------------------------------------
    def _anthropic(self, system, user, schema, effort, max_tokens) -> str | None:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(timeout=30.0, max_retries=1)
        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        try:
            response = self._client.beta.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
                output_config=output_config,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except anthropic.AuthenticationError:
            self.last_error, self.enabled = "authentication failed", False
            return None
        except anthropic.RateLimitError:
            self.last_error = "rate limited"
            return None
        except anthropic.APIStatusError as e:
            self.last_error = f"API error {e.status_code}"
            return None
        except anthropic.APIConnectionError:
            self.last_error = "connection error"
            return None
        if response.stop_reason == "refusal":
            self.last_error = "refused"
            return None
        return next((b.text for b in response.content if b.type == "text"), None)
