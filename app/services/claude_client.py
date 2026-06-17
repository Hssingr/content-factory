import json
import logging
import re
import time as _time

import anthropic

from app.services.model_routing import resolve_model

logger = logging.getLogger(__name__)


def parse_claude_json(text: str, required_keys: list[str], type_checks: dict | None = None) -> dict:
    """Shared JSON parser for all Claude responses.

    Strips accidental code fences, parses JSON, validates required keys,
    and optionally checks value types.

    Args:
        text:        Raw text from Claude.
        required_keys: Keys that must be present (raises ValueError if missing).
        type_checks: Optional dict mapping key → expected Python type.
                     e.g. {"issues": list, "overall_status": str}

    Returns:
        Parsed dict.

    Raises:
        ValueError: If JSON is malformed, a required key is absent,
                    or a type check fails.
    """
    cleaned = re.sub(r"```(?:json)?\s*([\s\S]*?)```", r"\1", text).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Claude JSON parse error: %s | Raw (first 300): %.300s", exc, text)
        raise ValueError(f"Claude returned invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Claude returned non-object JSON (got {type(data).__name__})")

    missing = [k for k in required_keys if k not in data]
    if missing:
        logger.error("Missing keys %s in Claude response: %.300s", missing, text)
        raise ValueError(f"Claude response missing required keys: {missing}")

    if type_checks:
        for key, expected_type in type_checks.items():
            if key in data and not isinstance(data[key], expected_type):
                raise ValueError(
                    f"Claude response key '{key}' expected {expected_type.__name__}, "
                    f"got {type(data[key]).__name__}"
                )

    # Log unexpected keys as warnings (CLAUDE.md: reject unknown keys unless explicitly allowed)
    if type_checks:
        known_keys = set(required_keys) | set(type_checks)
        extra = set(data.keys()) - known_keys
        if extra:
            logger.warning("Claude returned unexpected keys (ignored): %s", sorted(extra))

    return data


_MAX_RETRIES = 3
_BACKOFF_BASE = 2

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        import httpx
        from app.config import settings
        _client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            # connect=30s — default 5s triggers spurious APITimeoutError under API load.
            # read=600s (10 min) — storyboard segments can legitimately take 60-120s
            # to generate thousands of tokens; never cut them short.
            timeout=httpx.Timeout(timeout=600.0, connect=30.0),
        )
    return _client


def _make_system(system_prompt: str) -> list | str:
    """Apply cache_control: ephemeral when system prompt exceeds 800 chars."""
    if len(system_prompt) > 800:
        return [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},  # type: ignore[typeddict-unknown-key]
            }
        ]
    return system_prompt


def call_claude(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 1024,
    *,
    task: str,
    model_override: str | None = None,
) -> str:
    """Make a single-turn Claude API call. Shared entry point for all agents.

    Model is resolved via the routing table in ``model_routing.py`` using the
    required ``task`` keyword argument. Pass ``model_override`` only in tests or
    one-off scripts — prefer the routing table in production code.

    Applies ``cache_control: ephemeral`` automatically when the system prompt
    exceeds 800 characters.

    Args:
        system_prompt:  The system prompt text for this call.
        user_message:   The user turn content.
        max_tokens:     Maximum tokens in the response (default 1024).
        task:           Canonical task key — used for model routing and logging.
        model_override: Explicit model ID; bypasses routing (discouraged).

    Returns:
        Stripped text content of Claude's response.

    Raises:
        ValueError: If the response block is not text, the response is empty,
                    or ``task`` is not in MODEL_ROUTING.
        anthropic.RateLimitError: If all retry attempts are exhausted.
        anthropic.APIConnectionError: On network or config errors (not retried).
        anthropic.APIError: On any other non-retryable API error.
    """
    text, _usage = _call_claude_core(system_prompt, user_message, max_tokens, task, model_override)
    return text


def call_claude_with_usage(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 1024,
    *,
    task: str,
    model_override: str | None = None,
) -> tuple[str, dict]:
    """Make a single-turn Claude API call and also return token-usage diagnostics.

    Identical to ``call_claude`` but additionally returns the usage dict so callers
    that need to reason about output size (e.g. detecting truncation against
    ``max_tokens``) don't have to re-implement the retry/caching plumbing.

    Args:
        system_prompt:  The system prompt text for this call.
        user_message:   The user turn content.
        max_tokens:     Maximum tokens in the response (default 1024).
        task:           Canonical task key — used for model routing and logging.
        model_override: Explicit model ID; bypasses routing (discouraged).

    Returns:
        ``(text, usage)`` — ``text`` is the stripped response content; ``usage`` is
        ``{"input_tokens": int, "output_tokens": int, "cache_read_input_tokens": int}``
        from the LAST successful attempt.

    Raises:
        ValueError: If the response block is not text, the response is empty,
                    or ``task`` is not in MODEL_ROUTING.
        anthropic.RateLimitError: If all retry attempts are exhausted.
        anthropic.APIConnectionError: On network or config errors (not retried).
        anthropic.APIError: On any other non-retryable API error.
    """
    return _call_claude_core(system_prompt, user_message, max_tokens, task, model_override)


def _call_claude_core(
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    task: str,
    model_override: str | None = None,
) -> tuple[str, dict]:
    """Shared retry/caching/logging core for ``call_claude`` and ``call_claude_with_usage``.

    Returns:
        ``(text, usage)`` — see ``call_claude_with_usage``.
    """
    model = resolve_model(task, model_override)
    system = _make_system(system_prompt)
    cached_prompt = isinstance(system, list)

    prompt_chars = len(system_prompt) + len(user_message)
    logger.info(
        "call_claude start: task=%s model=%s cached_prompt=%s max_tokens=%d prompt_chars=%d (~%d tokens est.)",
        task, model, cached_prompt, max_tokens, prompt_chars, prompt_chars // 4,
    )

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _t0 = _time.monotonic()
        try:
            response = _get_client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            elapsed_ms = int((_time.monotonic() - _t0) * 1000)
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            }
            cached_hit = usage["cache_read_input_tokens"] > 0
            logger.info(
                "call_claude ok: task=%s model=%s attempt=%d elapsed_ms=%d "
                "input_tokens=%d output_tokens=%d cache_read=%d cached=%s",
                task, model, attempt + 1, elapsed_ms,
                usage["input_tokens"], usage["output_tokens"],
                usage["cache_read_input_tokens"], cached_hit,
            )
            block = response.content[0]
            if not isinstance(block, anthropic.types.TextBlock):
                raise ValueError(f"Unexpected response block type '{type(block).__name__}'")
            result = block.text.strip()
            if not result:
                raise ValueError("Empty response from Claude")
            return result, usage

        except (anthropic.RateLimitError, anthropic.APITimeoutError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_BASE ** attempt
                logger.warning("Transient error (%s), retrying in %ds", type(exc).__name__, wait)
                _time.sleep(wait)
        except anthropic.APIConnectionError as exc:
            logger.error("Connection error (config issue, not retrying): %s", exc)
            raise
        except anthropic.APIError as exc:
            logger.error("Claude API error: %s", exc)
            raise

    raise last_exc  # type: ignore[misc]


def call_claude_structured(
    *,
    task: str,
    system_prompt: str,
    user_message: "str | list[dict]",
    schema_name: str,
    input_schema: dict,
    max_tokens: int = 1024,
    model_override: str | None = None,
) -> dict:
    """Make a structured Claude call using forced tool use.

    Forces Claude to respond by filling the named tool's schema, guaranteeing
    structured JSON output without relying on text parsing or code-fence stripping.
    The ``parse_claude_json`` path is bypassed for these calls.

    Args:
        task:           Canonical task key — used for model routing and logging.
        system_prompt:  System prompt text (cache_control applied if > 800 chars).
        user_message:   User turn content — either a plain string or a content-block
                        list (e.g. ``[{"type": "text", "text": "..."}, {"type": "image",
                        "source": {"type": "url", "url": "..."}}]``) for vision calls.
        schema_name:    Name of the tool Claude will fill (e.g. "storyboard_batch").
        input_schema:   JSON Schema dict describing the tool's ``input`` parameters.
        max_tokens:     Maximum tokens in the response.
        model_override: Explicit model ID; bypasses routing (discouraged).

    Returns:
        The parsed ``input`` dict from Claude's ``tool_use`` response block.

    Raises:
        ValueError: If no ``tool_use`` block is present, task is unknown, or
                    the response is otherwise malformed.
        anthropic.RateLimitError: If all retry attempts are exhausted.
        anthropic.APIConnectionError: On network or config errors (not retried).
        anthropic.APIError: On any other non-retryable API error.
    """
    result, _usage = call_claude_structured_with_usage(
        task=task,
        system_prompt=system_prompt,
        user_message=user_message,
        schema_name=schema_name,
        input_schema=input_schema,
        max_tokens=max_tokens,
        model_override=model_override,
    )
    return result


def call_claude_structured_with_usage(
    *,
    task: str,
    system_prompt: str,
    user_message: "str | list[dict]",
    schema_name: str,
    input_schema: dict,
    max_tokens: int = 1024,
    model_override: str | None = None,
) -> tuple[dict, dict]:
    """Make a structured Claude call using forced tool use and also return token-usage diagnostics.

    Identical to ``call_claude_structured`` but additionally returns the usage dict so callers
    that need to reason about output size (e.g. detecting truncation against ``max_tokens``)
    don't have to re-implement the retry/caching plumbing.

    ``user_message`` accepts either a plain string or a content-block list for vision
    calls (e.g. interleaved text and image blocks). When a list is passed it is sent
    directly as the ``content`` array; strings are passed as-is (Anthropic treats them
    identically to ``[{"type": "text", "text": "..."}]``).

    Args:
        task:           Canonical task key — used for model routing and logging.
        system_prompt:  System prompt text (cache_control applied if > 800 chars).
        user_message:   User turn content — string or list of content blocks.
        schema_name:    Name of the tool Claude will fill (e.g. "storyboard_batch").
        input_schema:   JSON Schema dict describing the tool's ``input`` parameters.
        max_tokens:     Maximum tokens in the response.
        model_override: Explicit model ID; bypasses routing (discouraged).

    Returns:
        ``(result, usage)`` — ``result`` is the parsed ``input`` dict from Claude's
        ``tool_use`` response block; ``usage`` is
        ``{"input_tokens": int, "output_tokens": int, "cache_read_input_tokens": int}``
        from the last successful attempt.

    Raises:
        ValueError: If no ``tool_use`` block is present, task is unknown, or
                    the response is otherwise malformed.
        anthropic.RateLimitError: If all retry attempts are exhausted.
        anthropic.APIConnectionError: On network or config errors (not retried).
        anthropic.APIError: On any other non-retryable API error.
    """
    model = resolve_model(task, model_override)
    system = _make_system(system_prompt)
    cached_prompt = isinstance(system, list)

    tool = {
        "name": schema_name,
        "description": f"Structured output tool for task '{task}'",
        "input_schema": input_schema,
    }

    # For a list user_message (vision/multi-block), estimate chars from text blocks only
    if isinstance(user_message, list):
        text_chars = sum(
            len(b.get("text", ""))
            for b in user_message
            if isinstance(b, dict) and b.get("type") == "text"
        )
        image_count = sum(
            1 for b in user_message
            if isinstance(b, dict) and b.get("type") == "image"
        )
        prompt_chars = len(system_prompt) + text_chars
    else:
        text_chars = len(user_message)
        image_count = 0
        prompt_chars = len(system_prompt) + text_chars

    logger.info(
        "call_claude_structured start: task=%s model=%s schema=%s cached_prompt=%s "
        "max_tokens=%d prompt_chars=%d (~%d tokens est.) images=%d",
        task, model, schema_name, cached_prompt, max_tokens, prompt_chars,
        prompt_chars // 4, image_count,
    )

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _t0 = _time.monotonic()
        try:
            response = _get_client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=[tool],  # type: ignore[arg-type]
                tool_choice={"type": "tool", "name": schema_name},
                messages=[{"role": "user", "content": user_message}],
            )
            elapsed_ms = int((_time.monotonic() - _t0) * 1000)
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            }
            cached_hit = usage["cache_read_input_tokens"] > 0
            logger.info(
                "call_claude_structured ok: task=%s model=%s schema=%s attempt=%d "
                "elapsed_ms=%d input_tokens=%d output_tokens=%d cached=%s",
                task, model, schema_name, attempt + 1, elapsed_ms,
                usage["input_tokens"], usage["output_tokens"], cached_hit,
            )
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and block.name == schema_name:
                    return block.input, usage  # type: ignore[return-value]
            raise ValueError(
                f"call_claude_structured: no tool_use block for '{schema_name}' in response. "
                f"stop_reason={response.stop_reason}, "
                f"block_types={[getattr(b,'type','?') for b in response.content]}"
            )

        except (anthropic.RateLimitError, anthropic.APITimeoutError) as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_BASE ** attempt
                logger.warning("Transient error (%s), retrying in %ds", type(exc).__name__, wait)
                _time.sleep(wait)
        except anthropic.APIConnectionError as exc:
            logger.error("Connection error (not retrying): %s", exc)
            raise
        except anthropic.APIError as exc:
            logger.error("Claude API error: %s", exc)
            raise

    raise last_exc  # type: ignore[misc]


def call_claude_with_tools(
    system_prompt: str,
    user_message: str,
    tools: list[dict],
    max_tokens: int = 4096,
    max_rounds: int = 10,
    *,
    task: str,
    model_override: str | None = None,
) -> str:
    """Run an agentic Claude loop that may call tools multiple times.

    For Anthropic-native server-side tools (e.g. ``web_search_20250305``),
    execution happens on Anthropic's infrastructure. We continue the loop by
    acknowledging each tool_use with an empty ``tool_result`` content block
    until Claude reaches ``stop_reason = "end_turn"``.

    Args:
        system_prompt:  System prompt (cache_control applied if > 800 chars).
        user_message:   Initial user turn.
        tools:          Tool definitions, e.g. ``[{"type": "web_search_20250305", ...}]``.
        max_tokens:     Max tokens per Claude response.
        max_rounds:     Safety cap on tool-use iterations before raising.
        task:           Canonical task key — used for model routing and logging.
        model_override: Explicit model ID; bypasses routing (discouraged).

    Returns:
        Stripped text of Claude's final response.

    Raises:
        ValueError: If max_rounds is exceeded, no text block in final response,
                    or task is unknown.
        anthropic.APIError: On non-retryable API errors.
    """
    model = resolve_model(task, model_override)
    system = _make_system(system_prompt)

    logger.info(
        "call_claude_with_tools start: task=%s model=%s max_rounds=%d",
        task, model, max_rounds,
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]

    for round_num in range(max_rounds):
        try:
            response = _get_client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,  # type: ignore[arg-type]
                messages=messages,
            )
        except (anthropic.RateLimitError, anthropic.APITimeoutError) as exc:
            wait = _BACKOFF_BASE ** min(round_num, 2)
            logger.warning("Tool loop transient error (round %d): %s — retrying in %ds", round_num + 1, exc, wait)
            _time.sleep(wait)
            continue
        except anthropic.APIConnectionError as exc:
            logger.error("Tool loop connection error: %s", exc)
            raise
        except anthropic.APIError as exc:
            logger.error("Tool loop API error: %s", exc)
            raise

        block_types: dict[str, int] = {}
        for b in response.content:
            t = getattr(b, "type", type(b).__name__)
            block_types[t] = block_types.get(t, 0) + 1

        logger.info(
            "call_claude_with_tools round=%d task=%s model=%s stop_reason=%s "
            "blocks=%s input_tokens=%d output_tokens=%d",
            round_num + 1, task, model, response.stop_reason,
            dict(block_types),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if response.stop_reason == "tool_use" and tool_uses:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tu.id, "content": "Search completed."}
                    for tu in tool_uses
                ],
            })
            continue

        final_text = ""
        for b in response.content:
            block_type = getattr(b, "type", None)
            block_text = getattr(b, "text", None)
            if block_type == "text" and block_text and block_text.strip():
                final_text = block_text.strip()

        if not final_text:
            logger.error(
                "No usable text block in final response (stop_reason=%s). Block types: %s",
                response.stop_reason, dict(block_types),
            )
            raise ValueError("No text block in final Claude response")

        return final_text

    raise ValueError(f"call_claude_with_tools: exceeded max_rounds={max_rounds}")
