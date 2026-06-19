"""
LLM client for the OpenAI chat-completions API.
Sends HTTP POST to {LLM_BASE_URL}{LLM_GENERATE_PATH} (default
https://api.openai.com/v1/chat/completions) with a Bearer token and
parses the assistant message text from the response.
"""

import json
import logging
import re
import requests
from typing import Any

from config import (
    LLM_BASE_URL,
    LLM_GENERATE_PATH,
    LLM_MODEL,
    LLM_TIMEOUT,
    LLM_API_KEY,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
)

log = logging.getLogger(__name__)


class LLMClient:
    """HTTP client for the OpenAI chat-completions endpoint."""

    def __init__(self):
        self.url = f"{LLM_BASE_URL}{LLM_GENERATE_PATH}"
        self.model = LLM_MODEL
        self.timeout = LLM_TIMEOUT
        self.api_key = LLM_API_KEY
        self.temperature = LLM_TEMPERATURE
        self._http = requests.Session()

    def generate(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        """
        Call the LLM and return the raw text response.
        Raises LLMError on failure. When json_mode is set, asks the API for a
        guaranteed JSON object (response_format) — callers must instruct JSON.
        """
        if not self.api_key:
            raise LLMError(
                "No OpenAI API key configured. Set OPENAI_API_KEY (or LLM_API_KEY)."
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        # Only send temperature when explicitly configured — some newer models
        # reject any non-default value.
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # Cap output tokens. Reasoning models (gpt-5.x) use max_completion_tokens;
        # a generous value prevents the JSON plan from being truncated mid-response.
        if LLM_MAX_TOKENS > 0:
            payload["max_completion_tokens"] = LLM_MAX_TOKENS
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        log.info("LLM call: model=%s, prompt_len=%d", self.model, len(user_prompt))

        try:
            resp = self._http.post(
                self.url, json=payload, headers=headers, timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            # Surface OpenAI's error body, which carries the useful detail.
            detail = ""
            if e.response is not None:
                try:
                    detail = e.response.json().get("error", {}).get("message", "")
                except (ValueError, AttributeError):
                    detail = e.response.text[:300]
            raise LLMError(f"LLM request failed: {e}{f' — {detail}' if detail else ''}") from e
        except requests.RequestException as e:
            raise LLMError(f"LLM request failed: {e}") from e

        data = resp.json()

        # OpenAI chat-completions: {"choices": [{"message": {"content": "..."}}]}
        if "choices" in data:
            try:
                choice = data["choices"][0]
                content = choice["message"]["content"]
            except (KeyError, IndexError, TypeError) as e:
                raise LLMError(f"Malformed chat-completions response: {data}") from e
            # finish_reason="length" means the cap was hit and the output (often the
            # JSON plan) is truncated — surface it instead of returning broken JSON.
            if choice.get("finish_reason") == "length":
                raise LLMError(
                    "LLM response truncated (hit the output-token limit). "
                    "Increase LLM_MAX_TOKENS."
                )
            return content
        # Ollama-style fallback: {"response": "..."}
        if "response" in data:
            return data["response"]
        # Fallback: try other common keys
        for key in ("text", "output", "content", "result"):
            if key in data:
                return data[key]

        raise LLMError(f"Unexpected LLM response format: {list(data.keys())}")

    def generate_json(self, system_prompt: str, user_prompt: str) -> Any:
        """
        Call the LLM and parse the response as JSON.
        Uses JSON mode (response_format) and strips any markdown fences.
        """
        raw = self.generate(system_prompt, user_prompt, json_mode=True)
        return self._extract_json(raw)

    @staticmethod
    def _extract_json(text: str) -> Any:
        """Extract JSON from LLM output, handling markdown fences."""
        text = text.strip()

        # Remove markdown code fences
        fence_pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
        match = re.search(fence_pattern, text, re.DOTALL)
        if match:
            text = match.group(1).strip()

        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object/array in the text
        for pattern in [r'\{.*\}', r'\[.*\]']:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    continue

        raise LLMError(f"Could not parse JSON from LLM response: {text[:300]}")

    def close(self):
        self._http.close()


class LLMError(Exception):
    """Raised when LLM calls fail."""
    pass
