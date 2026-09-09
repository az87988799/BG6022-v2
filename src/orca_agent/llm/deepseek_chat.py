"""DeepSeek Chat Completions adapter with bounded, non-retrying HTTP calls."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from orca_agent.domain.p7_conversation import ContextSnapshot
from orca_agent.llm.ports import ModelCallRequest, ModelCallResponse, ModelMessage

DEFAULT_ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_RESPONSE_BYTES = 256 * 1024
HTTP_DEADLINE_SECONDS = 45.0
_PROJECT_ENV_KEYS = frozenset({"DEEPSEEK_API_KEY", "BG6022_P7_MODEL"})


class DeepSeekChatAdapter:
    """One request in, one response out; it has no database or tool access."""

    adapter_id = "deepseek_chat"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = HTTP_DEADLINE_SECONDS,
        transport: Callable[[ModelCallRequest, Mapping[str, object]], ModelCallResponse]
        | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = _runtime_environment(environ)
        self.api_key = api_key if api_key is not None else env.get("DEEPSEEK_API_KEY")
        self.model = model if model is not None else env.get("BG6022_P7_MODEL")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.last_request: ModelCallRequest | None = None
        self.last_request_body: dict[str, object] | None = None

    def interpret(self, context: ContextSnapshot) -> ModelCallResponse:
        if not self.api_key:
            return ModelCallResponse(
                provider="deepseek",
                model=self.model,
                error_code="configuration_missing",
                error_message="DEEPSEEK_API_KEY is not configured",
            )
        if not self.model:
            return ModelCallResponse(
                provider="deepseek",
                error_code="configuration_missing",
                error_message="BG6022_P7_MODEL is not configured",
            )
        request = ModelCallRequest.create(
            adapter_id=self.adapter_id,
            model=self.model,
            messages=(
                ModelMessage(role="system", content=_system_prompt()),
                ModelMessage(role="user", content=_context_prompt(context)),
            ),
            response_format={"type": "json_object"},
            max_tokens=2048,
            stream=False,
            thinking_enabled=False,
        )
        return self._send(request)

    def format_repair(
        self, context: ContextSnapshot, invalid_response: ModelCallResponse
    ) -> ModelCallResponse:
        """Ask the provider once to repair JSON shape, never scientific content."""

        if not self.api_key:
            return ModelCallResponse(
                provider="deepseek",
                model=self.model,
                error_code="configuration_missing",
                error_message="DEEPSEEK_API_KEY is not configured",
            )
        if not self.model:
            return ModelCallResponse(
                provider="deepseek",
                error_code="configuration_missing",
                error_message="BG6022_P7_MODEL is not configured",
            )
        original = (invalid_response.content or "")[:16_384]
        request = ModelCallRequest.create(
            adapter_id=self.adapter_id,
            model=self.model,
            messages=(
                ModelMessage(role="system", content=_system_prompt()),
                ModelMessage(
                    role="user",
                    content=(
                        "Repair the following invalid model output to the published P7 JSON "
                        "contract. Preserve only user-intent meaning; do not add IDs, approvals, "
                        "results, or scientific values. Return JSON only.\n"
                        + original
                        + "\nContext:\n"
                        + _context_prompt(context)
                    ),
                ),
            ),
            response_format={"type": "json_object"},
            max_tokens=2048,
            stream=False,
            thinking_enabled=False,
        )
        return self._send(request)

    def _send(self, request: ModelCallRequest) -> ModelCallResponse:
        self.last_request = request
        body = {
            "model": request.model,
            "messages": [item.model_dump(mode="json") for item in request.messages],
            "response_format": {"type": "json_object"},
            "max_tokens": request.max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        self.last_request_body = body
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        started = time.monotonic()
        try:
            if self.transport is not None:
                result = self.transport(request, {**headers, "body": body})
                if result.elapsed_ms is None:
                    return result.model_copy(
                        update={"elapsed_ms": int((time.monotonic() - started) * 1000)}
                    )
                return result
            return self._http(request, body, headers, started)
        except Exception as error:
            return ModelCallResponse(
                provider="deepseek",
                model=self.model,
                error_code="transport_error",
                error_message=str(error)[:512],
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def _http(
        self,
        request: ModelCallRequest,
        body: dict[str, object],
        headers: Mapping[str, str],
        started: float,
    ) -> ModelCallResponse:
        try:
            import httpx
        except ModuleNotFoundError:
            return ModelCallResponse(
                provider="deepseek",
                model=request.model,
                error_code="dependency_missing",
                error_message="P7 DeepSeek adapter requires the p7 optional dependency httpx",
            )
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        remaining = max(self.timeout_seconds - (time.monotonic() - started), 0.1)
        timeout = httpx.Timeout(remaining, connect=min(10.0, remaining))
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = client.post(self.endpoint, content=encoded, headers=headers)
            raw = response.content
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if len(raw) > MAX_RESPONSE_BYTES:
                return ModelCallResponse(
                    provider="deepseek",
                    model=request.model,
                    error_code="response_too_large",
                    error_message="DeepSeek response exceeded the 256 KiB limit",
                    raw_bytes=raw[:MAX_RESPONSE_BYTES],
                    elapsed_ms=elapsed_ms,
                )
            if response.status_code >= 400:
                code = (
                    "rate_limited"
                    if response.status_code == 429
                    else "unauthorized"
                    if response.status_code in {401, 403}
                    else "server_error"
                    if response.status_code >= 500
                    else "http_error"
                )
                return ModelCallResponse(
                    provider="deepseek",
                    model=request.model,
                    error_code=code,
                    error_message=raw.decode("utf-8", errors="replace")[:512],
                    raw_bytes=raw,
                    elapsed_ms=elapsed_ms,
                )
            try:
                document = json.loads(raw)
                choice = document["choices"][0]
                message = choice["message"]
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("response content is empty")
                usage = document.get("usage", {})
                request_id = document.get("id")
            except (TypeError, ValueError, KeyError, IndexError) as error:
                return ModelCallResponse(
                    provider="deepseek",
                    model=request.model,
                    error_code="invalid_provider_response",
                    error_message=str(error)[:512],
                    raw_bytes=raw,
                    elapsed_ms=elapsed_ms,
                )
            return ModelCallResponse(
                provider="deepseek",
                model=request.model,
                content=content,
                raw_bytes=raw,
                provider_request_id=request_id if isinstance(request_id, str) else None,
                usage=usage if isinstance(usage, dict) else {},
                elapsed_ms=elapsed_ms,
            )


def _system_prompt() -> str:
    prompt = _read_p7_resource("intake.prompt.txt")
    schema = _read_p7_resource("intake.schema.json")
    return f"{prompt}\n\nPublished JSON Schema:\n{schema}"


def _context_prompt(context: ContextSnapshot) -> str:
    payload = context.model_dump(mode="json")
    return (
        "Interpret this user message under the supplied context. JSON output is required:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _read_p7_resource(name: str) -> str:
    resource = Path(__file__).resolve().parents[1] / "resources" / "p7" / name
    return resource.read_text(encoding="utf-8")


def _runtime_environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """Resolve explicit process variables plus an optional project ``.env``.

    Tests and embedders can pass an explicit mapping to disable filesystem
    lookup.  For normal CLI use, a project-root ``.env`` is convenient while
    process variables remain authoritative when both are present.
    """

    if environ is not None:
        return environ
    values = dict(os.environ)
    env_path = _project_env_path()
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return values
    except OSError:
        return values
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        key, separator, raw_value = entry.partition("=")
        key = key.strip()
        if not separator or key not in _PROJECT_ENV_KEYS:
            continue
        if values.get(key):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _project_env_path() -> Path:
    current = Path.cwd() / ".env"
    if current.is_file():
        return current
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent / ".env"
    return current


__all__ = [
    "DEFAULT_ENDPOINT",
    "DeepSeekChatAdapter",
    "HTTP_DEADLINE_SECONDS",
    "MAX_RESPONSE_BYTES",
]
