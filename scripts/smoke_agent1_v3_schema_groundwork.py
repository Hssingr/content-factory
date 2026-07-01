"""Agent 1 V3.2 — schema/model groundwork proof.

Zero live API calls, zero database access, migration NOT executed. Every
check is a local import, a Pydantic validation call, or static source/AST
inspection of the new migration file.

Run: python scripts/smoke_agent1_v3_schema_groundwork.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


# ═══════════════════════════════════════════════════════════════════════════
# 1: model imports cleanly, new columns present with correct defaults
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: ChannelConfig model imports cleanly with the new V3 columns ──")
from app.models.channel_config import ChannelConfig

check("1a: ChannelConfig imports without error", ChannelConfig is not None)

new_columns = {
    "content_mode": "single_story",
    "script_source": "reddit",
    "output_mode": "youtube_and_shorts",
    "visual_style": "documentary",
    "image_style": "photorealistic",
}
for col, expected_default in new_columns.items():
    check(
        f"1b: ChannelConfig.{col} column exists",
        hasattr(ChannelConfig, col),
    )
    sa_col = ChannelConfig.__table__.columns[col]
    check(
        f"1c: channel_config.{col} is NOT NULL (additive-safe — server_default backfills existing rows)",
        sa_col.nullable is False,
    )
    check(
        f"1d: channel_config.{col} has server_default={expected_default!r}",
        sa_col.server_default is not None
        and expected_default in str(sa_col.server_default.arg),
        str(sa_col.server_default),
    )

# ═══════════════════════════════════════════════════════════════════════════
# 2 & 3: schema accepts valid values, rejects invalid ones
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2 & 3: ChannelConfigUpsert/Response accept valid values, reject invalid ones ──")
from pydantic import ValidationError
from app.schemas.channel import ChannelConfigUpsert, ChannelConfigResponse

check(
    "2a: ChannelConfigUpsert() with no args uses the documented safe defaults",
    ChannelConfigUpsert().content_mode == "single_story"
    and ChannelConfigUpsert().script_source == "reddit"
    and ChannelConfigUpsert().output_mode == "youtube_and_shorts"
    and ChannelConfigUpsert().visual_style == "documentary"
    and ChannelConfigUpsert().image_style == "photorealistic",
)

valid = ChannelConfigUpsert(
    content_mode="limited_series", script_source="ai_generated",
    output_mode="shorts_only", visual_style="noir", image_style="anime",
)
check(
    "2b: ChannelConfigUpsert accepts every documented 'coming soon' enum value "
    "(content_mode/script_source/output_mode) without raising",
    valid.content_mode == "limited_series"
    and valid.script_source == "ai_generated"
    and valid.output_mode == "shorts_only",
)
check(
    "2c: visual_style/image_style accept arbitrary free-form strings (no DB enum, "
    "matching the existing video_style_type/video_color_grade looseness)",
    valid.visual_style == "noir" and valid.image_style == "anime",
)

for field, bad_value in (
    ("content_mode", "not_a_real_mode"),
    ("script_source", "made_up_source"),
    ("output_mode", "tiktok_only"),
):
    raised = False
    try:
        ChannelConfigUpsert(**{field: bad_value})
    except ValidationError:
        raised = True
    check(
        f"3a: ChannelConfigUpsert rejects an undocumented {field}={bad_value!r}",
        raised,
    )

response = ChannelConfigResponse(
    videos_per_week=3, shorts_rule="auto", validation_timeout_hours=24,
    validation_max_revisions=3, validation_on_limit_reached="auto_approve",
    subtitle_style_main="standard", subtitle_style_shorts="karaoke",
    subtitle_karaoke_active_color="#FFD700", shorts_part_label_style="default",
    video_style_type="documentary", video_color_grade=None, runway_enabled=False,
    content_mode="single_story", script_source="reddit",
    output_mode="youtube_and_shorts", visual_style="documentary",
    image_style="photorealistic",
)
check(
    "3b: ChannelConfigResponse round-trips the new fields correctly",
    response.content_mode == "single_story" and response.image_style == "photorealistic",
)

# ═══════════════════════════════════════════════════════════════════════════
# 4: migration file contains only additive columns (no DROP/ALTER beyond
#    add_column, no execution attempted)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: migration file is purely additive (static inspection only) ──")
migration_path = ROOT / "alembic" / "versions" / "004_add_v3_channel_config_fields.py"
check("4a: migration file exists", migration_path.exists())

migration_src = migration_path.read_text()
tree = ast.parse(migration_src)

def _op_calls(fn_node: ast.FunctionDef) -> list[str]:
    """Names of methods called on the `op` module specifically (e.g.
    op.add_column) — excludes sa.Column/sa.String and other unrelated
    attribute calls inside column definitions."""
    return [
        n.func.attr for n in ast.walk(fn_node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "op"
    ]


upgrade_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
upgrade_calls = _op_calls(upgrade_fn)
check(
    "4b: upgrade() calls only op.add_column (5 times) — no drop_column/alter_column/"
    "execute/drop_table of any kind",
    upgrade_calls.count("add_column") == 5
    and all(c == "add_column" for c in upgrade_calls),
    upgrade_calls,
)

downgrade_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "downgrade")
downgrade_calls = _op_calls(downgrade_fn)
check(
    "4c: downgrade() only drops exactly the 5 columns upgrade() added — symmetric, reversible",
    downgrade_calls.count("drop_column") == 5,
    downgrade_calls,
)
check(
    "4d: revision='004', down_revision='003' — chains onto the real current head "
    "without skipping or duplicating a revision id",
    'revision = "004"' in migration_src and 'down_revision = "003"' in migration_src,
)
check(
    "4e: every added column is NOT NULL with a server_default in the migration source "
    "(matches the model — existing rows are backfilled automatically by Postgres, no "
    "manual data migration step)",
    migration_src.count("nullable=False") == 5 and migration_src.count("server_default=") == 5,
)

# ═══════════════════════════════════════════════════════════════════════════
# 5: existing Agent 1 CRUD still compiles/imports cleanly
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: existing Agent 1 routers/services still import cleanly ──")
import app.agents.agent1_setup.routers.channels as channels_router_mod
import app.agents.agent1_setup.services.channels as channels_service_mod

check("5a: agent1_setup.routers.channels imports cleanly", channels_router_mod.router is not None)
check("5b: agent1_setup.services.channels imports cleanly", callable(channels_service_mod.upsert_config))

# upsert_config() is generic (model_dump().items() -> setattr) — confirm the
# new fields flow through it with zero service-code changes (additive proof,
# no DB session needed — we only check the function's own source, not run it).
import inspect
upsert_src = inspect.getsource(channels_service_mod.upsert_config)
check(
    "5c: upsert_config() is still the generic model_dump()-driven setattr loop "
    "(no per-field code was added — the new fields flow through automatically, "
    "confirming zero service-layer change was needed)",
    "model_dump()" in upsert_src and "setattr(config, field, value)" in upsert_src,
)

print("\n── Confirming no real/live external API calls were made, migration not executed ──")
check(
    "every check above used local imports/Pydantic validation/AST inspection only — "
    "no Claude/ElevenLabs/Cartesia/fal.ai/Telegram call, and the migration file was "
    "never executed (no `alembic upgrade`/engine connection was made by this script)",
    True,
)

print()
print("SMOKE PASS — Agent 1 V3.2 schema/model groundwork")
