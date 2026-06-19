"""Smoke test for parse_claude_json strict unknown-key behavior.

Zero API calls, zero DB access.
Run: python scripts/smoke_parse_claude_json_strict.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.claude_client import parse_claude_json


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK - {label}")


def expect_value_error(label: str, func) -> None:
    try:
        func()
    except ValueError:
        print(f"OK - {label}")
        return
    raise AssertionError(label)


expect_value_error(
    "unknown keys reject by default",
    lambda: parse_claude_json(
        '{"title":"A","extra":true}',
        required_keys=["title"],
        type_checks={"title": str},
        allowed_keys=["title"],
    ),
)

allowed = parse_claude_json(
    '{"title":"A","extra":true}',
    required_keys=["title"],
    type_checks={"title": str},
    allowed_keys=["title"],
    allow_extra_keys=True,
)
check("unknown keys pass when allow_extra_keys=True", allowed["extra"] is True)

unrestricted = parse_claude_json(
    '{"title":"A","extra":true}',
    required_keys=["title"],
    type_checks={"title": str},
)
check("no allowed_keys means no extra-key check", unrestricted["extra"] is True)

print("SMOKE PASS - parse_claude_json strict unknown-key behavior")
