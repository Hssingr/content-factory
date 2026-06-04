import json
import logging
import re
import time as _time

import anthropic

from app.config import settings

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
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def call_claude(system_prompt: str, user_message: str, max_tokens: int = 1024) -> str:
    """Make a single-turn Claude API call. Shared entry point for all agents.

    Applies cache_control: ephemeral automatically when the system prompt exceeds
    800 characters (approaching the 1024-token minimum required for caching on Sonnet).

    Args:
        system_prompt: The system prompt text for this call.
        user_message: The user turn content.
        max_tokens: Maximum tokens in the response (default 1024).

    Returns:
        Stripped text content of Claude's response.

    Raises:
        ValueError: If the response block is not text or the response is empty.
        anthropic.RateLimitError: If all retry attempts are exhausted.
        anthropic.APIConnectionError: On network or config errors (not retried).
        anthropic.APIError: On any other non-retryable API error.
    """
    if len(system_prompt) > 800:
        system: list | str = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},  # type: ignore[typeddict-unknown-key]
            }
        ]
    else:
        system = system_prompt

    prompt_chars = len(system_prompt) + len(user_message)
    logger.info(
        "call_claude start: model=%s max_tokens=%d prompt_chars=%d (~%d tokens est.)",
        settings.claude_model, max_tokens, prompt_chars, prompt_chars // 4,
    )

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        _t0 = _time.monotonic()
        try:
            response = _get_client().messages.create(
                model=settings.claude_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            elapsed_ms = int((_time.monotonic() - _t0) * 1000)
            logger.info(
                "call_claude ok: attempt=%d elapsed_ms=%d input_tokens=%d output_tokens=%d cache_read=%d",
                attempt + 1,
                elapsed_ms,
                response.usage.input_tokens,
                response.usage.output_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0),
            )
            block = response.content[0]
            if not isinstance(block, anthropic.types.TextBlock):
                raise ValueError(f"Unexpected response block type '{type(block).__name__}'")
            result = block.text.strip()
            if not result:
                raise ValueError("Empty response from Claude")
            return result

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


def call_claude_with_tools(
    system_prompt: str,
    user_message: str,
    tools: list[dict],
    max_tokens: int = 4096,
    max_rounds: int = 10,
) -> str:
    """Run an agentic Claude loop that may call tools multiple times.

    For Anthropic-native server-side tools (e.g. ``web_search_20250305``),
    execution happens on Anthropic's infrastructure. We continue the loop by
    acknowledging each tool_use with an empty ``tool_result`` content block
    until Claude reaches ``stop_reason = "end_turn"``.

    Args:
        system_prompt: System prompt (cache_control applied if > 800 chars).
        user_message:  Initial user turn.
        tools:         Tool definitions, e.g. ``[{"type": "web_search_20250305", ...}]``.
        max_tokens:    Max tokens per Claude response.
        max_rounds:    Safety cap on tool-use iterations before raising.

    Returns:
        Stripped text of Claude's final response.

    Raises:
        ValueError: If max_rounds is exceeded or no text block in final response.
        anthropic.APIError: On non-retryable API errors.
    """
    if len(system_prompt) > 800:
        system: list | str = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},  # type: ignore[typeddict-unknown-key]
            }
        ]
    else:
        system = system_prompt

    messages: list[dict] = [{"role": "user", "content": user_message}]

    for round_num in range(max_rounds):
        try:
            response = _get_client().messages.create(
                model=settings.claude_model,
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

        # Count content block types for logging (don't dump full content — too verbose)
        block_types = {}
        for b in response.content:
            t = getattr(b, 'type', type(b).__name__)
            block_types[t] = block_types.get(t, 0) + 1

        logger.info(
            "call_claude_with_tools round=%d stop_reason=%s blocks=%s input_tokens=%d output_tokens=%d",
            round_num + 1,
            response.stop_reason,
            dict(block_types),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        # ── pause_turn: Anthropic executed a server-side tool (web_search_20250305)
        # and has embedded the results in response.content. Append the assistant
        # message and re-call — Anthropic injects the search results automatically.
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        # ── tool_use: Claude wants to call a client-side tool (rare with web_search)
        tool_uses = [b for b in response.content if getattr(b, 'type', None) == "tool_use"]
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

        # ── end_turn (or any other stop_reason): extract the final text answer.
        # Only look at blocks with type=="text" and non-empty .text; skip
        # server_tool_use, web_search_tool_result, and other structured blocks.
        final_text = ""
        for b in response.content:
            block_type = getattr(b, 'type', None)
            block_text = getattr(b, 'text', None)
            if block_type == "text" and block_text and block_text.strip():
                final_text = block_text.strip()

        if not final_text:
            logger.error(
                "No usable text block in final response (stop_reason=%s). "
                "Block types: %s",
                response.stop_reason,
                dict(block_types),
            )
            raise ValueError("No text block in final Claude response")

        return final_text

    raise ValueError(f"call_claude_with_tools: exceeded max_rounds={max_rounds}")
