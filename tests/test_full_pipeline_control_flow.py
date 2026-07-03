import inspect
import unittest

import test_pipeline.test_full_pipeline as pipeline


class TestFullPipelineControlFlow(unittest.TestCase):
    def test_parent_visual_failure_gates_child_visuals_and_render(self):
        src = inspect.getsource(pipeline._execute_audio_through_render)
        self.assertIn("parent_visuals_ok = _run_step_visuals(parent", src)
        self.assertIn("Skipping child visuals + render", src)
        self.assertLess(src.index("parent_visuals_ok = _run_step_visuals(parent"), src.index("STEP 7: Child visuals"))


if __name__ == "__main__":
    unittest.main()
