"""
LLM client for the local gpt-20b model at 10.10.17.55:80.
Sends HTTP POST to /api/generate and parses JSON from the response.
"""

import json
import logging
import re
import requests
from typing import Any, Optional

from config import LLM_BASE_URL, LLM_GENERATE_PATH, LLM_MODEL, LLM_TIMEOUT

log = logging.getLogger(__name__)


class LLMClient:
    """HTTP client for the local LLM endpoint."""

    def __init__(self):
        self.url = f"{LLM_BASE_URL}{LLM_GENERATE_PATH}"
        self.model = LLM_MODEL
        self.timeout = LLM_TIMEOUT
        self._http = requests.Session()

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """
        Call the LLM and return the raw text response.
        Raises LLMError on failure.
        """
        payload = {
            "model": self.model,
            "prompt": user_prompt,
            "system": system_prompt,
            "stream": False,
        }

        log.info("LLM call: model=%s, prompt_len=%d", self.model, len(user_prompt))

        try:
            resp = self._http.post(
                self.url, json=payload, timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise LLMError(f"LLM request failed: {e}") from e

        data = resp.json()

        # Handle different response formats
        # Ollama-style: {"response": "..."}
        if "response" in data:
            return data["response"]
        # OpenAI-style: {"choices": [{"message": {"content": "..."}}]}
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        # Fallback: try 'text' or 'output'
        for key in ("text", "output", "content", "result"):
            if key in data:
                return data[key]

        raise LLMError(f"Unexpected LLM response format: {list(data.keys())}")

    def generate_json(self, system_prompt: str, user_prompt: str) -> Any:
        """
        Call the LLM and parse the response as JSON.
        Strips markdown fences and whitespace before parsing.
        """
        raw = self.generate(system_prompt, user_prompt)
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
