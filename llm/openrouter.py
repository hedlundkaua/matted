import os
import json
from .base import LLMProvider


DEFAULT_OPENROUTER_STREAM = "1"

class OpenRouterProvider(LLMProvider):
    """
    Provider for OpenRouter API.
    Implements HTTP requests to the OpenRouter endpoint.
    """

    def __init__(self):
        self.api_key = os.environ.get("OPENROUTER_API_KEY")
        self.model = os.environ.get("OPENROUTER_MODEL", "google/gemma-4-31b-it")
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

        if not self.api_key:
            # We don't raise here to avoid crashing the whole system at init,
            # but we'll fail during generate().
            pass

    def _stream_enabled(self) -> bool:
        raw = os.environ.get("MATTED_OPENROUTER_STREAM", DEFAULT_OPENROUTER_STREAM)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _payload(self, prompt: str, *, stream: bool) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }
        if stream:
            payload["stream"] = True
        return payload

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost", # Required by some OpenRouter models
            "X-Title": "Dynamic Swarm Local"
        }

    def generate(self, prompt: str) -> str:
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set in environment variables.")

        if self._stream_enabled():
            try:
                return self._generate_stream(prompt)
            except Exception as stream_error:
                print(f"[openrouter] streaming failed, falling back to non-stream response: {stream_error}", flush=True)

        return self._generate_non_stream(prompt)

    def _generate_non_stream(self, prompt: str) -> str:
        import urllib.request

        data = json.dumps(self._payload(prompt, stream=False)).encode("utf-8")

        try:
            req = urllib.request.Request(self.base_url, data=data, headers=self._headers(), method="POST")
            with urllib.request.urlopen(req) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                # Extract the content from the OpenAI-compatible response structure
                return res_data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            raise RuntimeError(f"OpenRouter API request failed: {e}")

    def _generate_stream(self, prompt: str) -> str:
        import urllib.request

        data = json.dumps(self._payload(prompt, stream=True)).encode("utf-8")
        req = urllib.request.Request(self.base_url, data=data, headers=self._headers(), method="POST")
        chunks: list[str] = []

        with urllib.request.urlopen(req) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_text = line[len("data:"):].strip()
                if data_text == "[DONE]":
                    break
                token = self._extract_stream_delta(data_text)
                if token:
                    print(token, end="", flush=True)
                    chunks.append(token)
        if chunks:
            print(flush=True)
        return "".join(chunks).strip()

    def _extract_stream_delta(self, data_text: str) -> str:
        event = json.loads(data_text)
        choices = event.get("choices")
        if not choices:
            return ""
        first = choices[0]
        delta = first.get("delta") if isinstance(first, dict) else None
        if isinstance(delta, dict):
            content = delta.get("content")
            if content is not None:
                return str(content)
        message = first.get("message") if isinstance(first, dict) else None
        if isinstance(message, dict) and message.get("content") is not None:
            return str(message["content"])
        text = first.get("text") if isinstance(first, dict) else None
        return str(text) if text is not None else ""
