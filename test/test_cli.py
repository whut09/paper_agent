import importlib
import sys
import unittest


class TestCliVersion(unittest.TestCase):
    def tearDown(self):
        for module_name in [
            "paper_agent",
            "paper_agent.paper_agent",
            "paper_agent.high_level",
            "paper_agent.doclayout",
        ]:
            sys.modules.pop(module_name, None)

    def test_importing_package_does_not_eagerly_load_translation_pipeline(self):
        pkg = importlib.import_module("paper_agent")

        self.assertEqual(pkg.__version__, "1.9.11")
        self.assertNotIn("paper_agent.high_level", sys.modules)

    def test_version_flag_exits_before_loading_heavy_modules(self):
        cli = importlib.import_module("paper_agent.paper_agent")

        self.assertNotIn("paper_agent.high_level", sys.modules)
        self.assertNotIn("paper_agent.doclayout", sys.modules)

        with self.assertRaises(SystemExit) as exit_context:
            cli.main(["-v"])

        self.assertEqual(exit_context.exception.code, 0)
        self.assertNotIn("paper_agent.high_level", sys.modules)
        self.assertNotIn("paper_agent.doclayout", sys.modules)


if __name__ == "__main__":
    unittest.main()
