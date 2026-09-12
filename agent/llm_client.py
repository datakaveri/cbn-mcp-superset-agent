"""
LLM client for OpenAI-compatible chat-completions APIs.
Sends HTTP POST to {LLM_BASE_URL}{LLM_GENERATE_PATH} (default
https://api.openai.com/v1/chat/completions) with a Bearer token and
parses the assistant message text from the response.

Amazon Bedrock Mantle works unchanged: point LLM_BASE_URL at
https://bedrock-mantle.<region>.api.aws/openai/v1 and use a Bedrock API key.
When LLM_PROJECT_ID is set it goes out as the OpenAI-Project header, which
Mantle uses to attribute the request to a Bedrock project.

Callers can pass a JSON schema (sent as a strict Structured Outputs
response_format) and a per-call reasoning_effort. If the endpoint rejects
either, the call is retried once in plain JSON mode without them.
"""

import json
import logging
import re
import requests
from typing import Any, Optional

from config import (
    LLM_BASE_URL,
    LLM_GENERATE_PATH,
    LLM_MODEL,
    LLM_TIMEOUT,
    LLM_API_KEY,
    LLM_PROJECT_ID,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_STRUCTURED_OUTPUTS,
)

log = logging.getLogger(__name__)


class LLMClient:
    """HTTP client for an OpenAI-compatible chat-completions endpoint."""

    def __init__(self):
        self.url = f"{LLM_BASE_URL}{LLM_GENERATE_PATH}"
        self.model = LLM_MODEL
        self.timeout = LLM_TIMEOUT
        self.api_key = LLM_API_KEY
        self.project = LLM_PROJECT_ID
        self.temperature = LLM_TEMPERATURE
        self._http = requests.Session()

    def generate(self, system_prompt: str, user_prompt: str, json_mode: bool = False,
                 schema: Optional[dict] = None, schema_name: str = "response",
                 reasoning_effort: Optional[str] = None) -> str:
        """
        Call the LLM and return the raw text response. Raises LLMError on failure.
        json_mode asks for a JSON object; a `schema` asks for strict Structured
        Outputs matching it (the prompt still describes the task). reasoning_effort
        (none/low/medium/high/...) is sent only when given.
        """
        if not self.api_key:
            raise LLMError(
                "No LLM API key configured. Set OPENAI_API_KEY (or LLM_API_KEY)."
            )

        strict = schema is not None and LLM_STRUCTURED_OUTPUTS
        system = system_prompt
        if schema is not None and not strict:
            system += self._schema_hint(schema)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
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
        if strict:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        elif json_mode or schema is not None:
            payload["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.project:
            headers["OpenAI-Project"] = self.project

        log.info("LLM call: model=%s, prompt_len=%d, schema=%s, effort=%s",
                 self.model, len(user_prompt), schema_name if schema else None, reasoning_effort)

        try:
            resp = self._post(payload, headers)
        except LLMError as e:
            if e.status != 400 or not (strict or "reasoning_effort" in payload):
                raise
            # Some OpenAI-compatible endpoints reject strict schemas or
            # reasoning_effort. Retry once in plain JSON mode without them, with the
            # schema spelled out in the prompt so the shape stays the same.
            log.warning("LLM endpoint rejected structured output or reasoning_effort (%s); "
                        "retrying in plain JSON mode", e)
            payload.pop("reasoning_effort", None)
            if strict:
                payload["response_format"] = {"type": "json_object"}
                payload["messages"][0]["content"] = system_prompt + self._schema_hint(schema)
            resp = self._post(payload, headers)

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

    def _post(self, payload: dict, headers: dict):
        """POST the payload; raise LLMError (with the HTTP status) on failure."""
        try:
            resp = self._http.post(self.url, json=payload, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            # Surface the API's error body, which carries the useful detail.
            detail = self._error_detail(e.response) if e.response is not None else ""
            status = e.response.status_code if e.response is not None else None
            raise LLMError(f"LLM request failed: {e}{f' — {detail}' if detail else ''}", status=status) from e
        except requests.RequestException as e:
            raise LLMError(f"LLM request failed: {e}") from e

    @staticmethod
    def _schema_hint(schema: dict) -> str:
        """Spell the schema out in the prompt when strict mode isn't used."""
        return ("\n\nRespond with one JSON object matching this JSON schema:\n"
                + json.dumps(schema, separators=(",", ":")))

    @staticmethod
    def _error_detail(response) -> str:
        """The message from an error body. OpenAI nests it under error.message;
        other OpenAI-compatible APIs may send error as a string or a top-level
        message, so try those before falling back to the raw text."""
        try:
            body = response.json()
        except ValueError:
            return response.text[:300]
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str) and err:
                return err
            if body.get("message"):
                return str(body["message"])
        return response.text[:300]

    def generate_json(self, system_prompt: str, user_prompt: str, schema: Optional[dict] = None,
                      schema_name: str = "response", reasoning_effort: Optional[str] = None) -> Any:
        """
        Call the LLM and parse the response as JSON: strict Structured Outputs when
        a schema is given, JSON mode otherwise. Strips any markdown fences.
        """
        raw = self.generate(system_prompt, user_prompt, json_mode=True, schema=schema,
                            schema_name=schema_name, reasoning_effort=reasoning_effort)
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
    """Raised when LLM calls fail. `status` is the HTTP status when there was one."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status
