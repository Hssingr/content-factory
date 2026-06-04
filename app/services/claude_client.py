import logging
import time

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

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

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = _get_client().messages.create(
                model=settings.claude_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            logger.debug(
                "call_claude attempt=%d cache_read=%s input_tokens=%s",
                attempt + 1,
                getattr(response.usage, "cache_read_input_tokens", 0),
                response.usage.input_tokens,
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
                time.sleep(wait)
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
            time.sleep(wait)
            continue
        except anthropic.APIConnectionError as exc:
            logger.error("Tool loop connection error: %s", exc)
            raise
        except anthropic.APIError as exc:
            logger.error("Tool loop API error: %s", exc)
            raise

        logger.debug(
            "call_claude_with_tools round=%d stop_reason=%s input_tokens=%s",
            round_num + 1, response.stop_reason, response.usage.input_tokens,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # Dump the first block's full structure to understand the web_search format
        if round_num == 0 or response.stop_reason != "tool_use":
            for i, b in enumerate(response.content[:3]):
                try:
                    dump = b.model_dump() if hasattr(b, 'model_dump') else vars(b)
                except Exception:
                    dump = str(b)
                logger.info("Block[%d]: %s", i, dump)

        logger.info(
            "call_claude_with_tools round=%d stop_reason=%s tool_uses=%d content_types=%s",
            round_num + 1,
            response.stop_reason,
            len(tool_uses),
            [type(b).__name__ for b in response.content],
        )

        if response.stop_reason != "tool_use" or not tool_uses:
            # Try to find ANY text across all possible attributes
            # (web_search blocks may store text in non-standard locations)
            final_text = ""
            for b in response.content:
                # Check .text (standard TextBlock)
                t = getattr(b, 'text', None)
                if t:
                    final_text = t
                    continue
                # Check inside nested structures (e.g. web search result content)
                for attr in ('content', 'output', 'result'):
                    val = getattr(b, attr, None)
                    if isinstance(val, str) and val:
                        final_text = val
                        break
                    if isinstance(val, list):
                        for item in val:
                            t = getattr(item, 'text', None) or (item.get('text') if isinstance(item, dict) else None)
                            if t:
                                final_text = t

            if not final_text:
                logger.error(
                    "No usable text in final response. content=%s",
                    [(type(b).__name__, b.model_dump() if hasattr(b, 'model_dump') else str(b))
                     for b in response.content[:5]],
                )
                raise ValueError("No text block in final Claude response")
            return final_text.strip()

        # Continue loop: acknowledge each tool_use.
        # For server-side tools (web_search_20250305), Anthropic executes the search;
        # we pass "Search completed." so Claude knows to proceed with the results it has.
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tu.id, "content": "Search completed."}
                for tu in tool_uses
            ],
        })

    raise ValueError(f"call_claude_with_tools: exceeded max_rounds={max_rounds}")
