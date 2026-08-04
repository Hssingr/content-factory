"""Runtime proof for the Agent 6 roadmap's Phase A (code_report/agent6_metadata_roadmap.md):
`_call_fal()` / `generate_beat_image()` and the fal.ai model-capability/payload/cache-key
machinery moved from `app.agents.agent4_visuals.services.flux_generator` /
`.image_router` into the shared `app.services.flux_client` module.

This is a pure relocation — no behavior change. These tests prove:
  1. Every name `image_router.py` used to own and that a test/caller might still
     reach via `image_router.<name>` resolves to the exact same object now
     living in `app.services.flux_client` (re-export identity, not a copy).
  2. `flux_generator.py`'s aliased imports (`_DEFAULT_WIDTH`, `_DEFAULT_HEIGHT`,
     `_DEFAULT_MODEL_KEY`, `_ENV_SAFE_PROMPTS`, `generate_beat_image`) resolve to
     the shared module's real values/objects, so every remaining call site in
     that file needs zero further changes.
  3. `app.services.flux_client` has zero imports from any `app.agents.*`
     package — the whole point of the extraction (CLAUDE.md §11.5's "one
     fal.ai integration point" rule combined with the cross-agent-import
     boundary) is that a future `app.agents.agent6_metadata` package can
     import this shared module with no circular-import risk back into Agent 4.
  4. `generate_beat_image_with_routing()` (which stays in Agent 4) is
     unaffected — same signature, same behavior, still callable.

No live external API calls anywhere in this file (CLAUDE.md §19.1) — this is
static/import-level verification only, no fal.ai/Claude call is made.
"""

import ast
import inspect
import unittest


class ReExportIdentityTest(unittest.TestCase):
    """image_router.py must re-export the exact same objects flux_client.py owns,
    not copies — so every existing `image_router.<name>` caller (production or
    test) is unaffected by the relocation."""

    def test_image_router_reexports_are_identical_objects(self) -> None:
        import app.services.flux_client as fc
        import app.agents.agent4_visuals.services.image_router as ir

        self.assertIs(ir.MODEL_CAPABILITIES, fc.MODEL_CAPABILITIES)
        self.assertIs(ir.build_fal_payload, fc.build_fal_payload)
        self.assertIs(ir.build_cache_key_material, fc.build_cache_key_material)
        self.assertIs(ir.ModelKey, fc.ModelKey)
        self.assertIs(ir.SizeMode, fc.SizeMode)
        self.assertIs(ir.SafetyMode, fc.SafetyMode)

    def test_image_router_own_routing_logic_untouched(self) -> None:
        """select_route()/ImageRoute/the Dev-Pro heuristic are genuinely
        beat-specific and were never part of the extraction — confirm they
        still live in image_router.py itself, not re-exported from elsewhere."""
        import app.services.flux_client as fc
        import app.agents.agent4_visuals.services.image_router as ir

        self.assertFalse(hasattr(fc, "select_route"))
        self.assertFalse(hasattr(fc, "ImageRoute"))
        self.assertTrue(callable(ir.select_route))
        route = ir.select_route({"beat_order": 0}, "content-123")
        self.assertEqual(route.model_key, "schnell")  # routing disabled by default


class FluxGeneratorAliasedImportsTest(unittest.TestCase):
    """flux_generator.py's remaining functions (generate_beat_image_with_routing,
    generate_all_beat_images, _build_defect_rewrite_prompt, etc.) reference
    _DEFAULT_WIDTH/_DEFAULT_HEIGHT/_DEFAULT_MODEL_KEY/_ENV_SAFE_PROMPTS by their
    original underscore-prefixed names — confirm the aliased import makes those
    names resolve to the shared module's real values, not stale duplicates."""

    def test_aliased_constants_match_shared_module(self) -> None:
        import app.services.flux_client as fc
        import app.agents.agent4_visuals.services.flux_generator as fg

        self.assertEqual(fg._DEFAULT_WIDTH, fc.DEFAULT_WIDTH)
        self.assertEqual(fg._DEFAULT_HEIGHT, fc.DEFAULT_HEIGHT)
        self.assertEqual(fg._DEFAULT_MODEL_KEY, fc.DEFAULT_MODEL_KEY)
        self.assertEqual(fg._DEFAULT_WIDTH, 1920)
        self.assertEqual(fg._DEFAULT_HEIGHT, 1080)
        self.assertEqual(fg._DEFAULT_MODEL_KEY, "schnell")
        self.assertIs(fg._ENV_SAFE_PROMPTS, fc.ENV_SAFE_PROMPTS)

    def test_generate_beat_image_is_the_shared_function(self) -> None:
        import app.services.flux_client as fc
        import app.agents.agent4_visuals.services.flux_generator as fg

        self.assertIs(fg.generate_beat_image, fc.generate_beat_image)

    def test_call_fal_no_longer_defined_locally(self) -> None:
        """_call_fal() moved entirely — flux_generator.py has no local copy,
        confirming this isn't a fork (one implementation, not two)."""
        import app.agents.agent4_visuals.services.flux_generator as fg

        self.assertFalse(hasattr(fg, "_call_fal"))

    def test_generate_beat_image_with_routing_unaffected(self) -> None:
        """The one function that stays in Agent 4 and calls generate_beat_image
        internally — same signature as before the extraction."""
        import app.agents.agent4_visuals.services.flux_generator as fg

        sig = inspect.signature(fg.generate_beat_image_with_routing)
        self.assertEqual(
            list(sig.parameters), ["beat", "content_id", "tier_counts", "width", "height"],
        )
        self.assertEqual(sig.parameters["width"].default, 1920)
        self.assertEqual(sig.parameters["height"].default, 1080)


class NoCrossAgentImportTest(unittest.TestCase):
    """The whole point of Phase A: a future app.agents.agent6_metadata package
    must be able to import app.services.flux_client with zero circular-import
    risk back into app.agents.agent4_visuals. Static AST check — no runtime
    import needed to prove this, and it can never be defeated by a lazy
    (function-body) import that a runtime check would miss."""

    def test_flux_client_has_no_agent_package_imports(self) -> None:
        with open("app/services/flux_client.py") as f:
            source = f.read()
        tree = ast.parse(source)
        agent_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("app.agents"):
                    agent_imports.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.agents"):
                        agent_imports.append(alias.name)
        self.assertEqual(
            agent_imports, [],
            f"app/services/flux_client.py must not import from any app.agents.* "
            f"package (found: {agent_imports}) — this is the shared client a "
            f"future Agent 6 depends on without touching Agent 4's package.",
        )

    def test_flux_client_importable_standalone(self) -> None:
        """A plain, real import (not just AST inspection) — proves there is no
        runtime circular-import failure either, not just no static import
        statement."""
        import app.services.flux_client as fc

        self.assertTrue(callable(fc.generate_beat_image))
        self.assertTrue(callable(fc.build_fal_payload))
        self.assertTrue(callable(fc.build_cache_key_material))
        self.assertIn("schnell", fc.MODEL_CAPABILITIES)
        self.assertIn("dev", fc.MODEL_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
