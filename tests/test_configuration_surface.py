"""Configuration/documentation surface checks without production credentials."""
import ast
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import setup_env

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationSurfaceTests(unittest.TestCase):
    def test_template_and_wizard_have_unique_declared_fields(self):
        tree = ast.parse((ROOT / "config.py").read_text())
        declared = set()
        for node in tree.body:
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets if isinstance(node, ast.Assign) else []
            declared.update(target.id for target in targets if isinstance(target, ast.Name))
        template = re.findall(r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M)
        wizard = [row[0] for _, items in setup_env.SECTIONS for row in items]
        for fields in (template, wizard):
            self.assertEqual(len(fields), len(set(fields)), "Duplicate configuration fields")
            self.assertFalse(set(fields) - declared, "Undeclared configuration fields")
        for retired in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
                        "DASHSCOPE_API_KEY", "SUB_AGENT_API_KEY", "MAIN_AGENT_API_KEY",
                        "TAVILY_MAX_REQUESTS_PER_RUN", "PRIORITY_RESEARCH_MAX_LINKS_PER_PAGE"):
            self.assertNotIn(retired, template + wizard)

    def test_documented_common_cloud_controls_are_actually_mapped(self):
        workflow = (ROOT / ".github/workflows/weekly-radar.yml").read_text()
        for name in ("FOLLOW_BUILDERS_ENABLED", "FOLLOW_BUILDERS_FEED_URL",
                     "TAVILY_SOCIAL_MAX_RESULTS", "PARADIGM_ALLOW_UPDATES", "LOG_LEVEL"):
            self.assertRegex(workflow, re.escape(name) + r": \$\{\{ vars\." + re.escape(name) + r"(?: \|\|| \}\})")

    def test_status_does_not_claim_model_defaults_when_roles_are_missing(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True), \
                patch.object(setup_env, "ENV_PATH", Path(temporary) / "missing.env"), \
                patch("agents.llm_utils.resolve_all", return_value=(SimpleNamespace(label="sub-missing"), SimpleNamespace(label="main-missing"))):
            output = io.StringIO()
            with redirect_stdout(output):
                setup_env._print_status()
            status = output.getvalue()
            self.assertIn("必填，不继承其他配置", status)
            self.assertIn("sub-missing", status)
            self.assertNotIn("默认: qwen3.7-plus", status)

    def test_status_prefers_process_values_and_never_prints_key_fragments(self):
        process = {"SUB_AGENT_PROVIDER": "custom-provider", "SUB_AGENT_MODEL": "process-model",
                   "SUB_AGENT_LLM_API_KEY": "private-api-token-do-not-log"}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, process, clear=True), \
                patch.object(setup_env, "ENV_PATH", Path(temporary) / "missing.env"), \
                patch.object(setup_env, "_load_current", return_value={"SUB_AGENT_MODEL": "file-model"}), \
                patch("agents.llm_utils.resolve_all", return_value=(SimpleNamespace(label="sub"), SimpleNamespace(label="main"))):
            output = io.StringIO()
            with redirect_stdout(output):
                setup_env._print_status()
            status = output.getvalue()
            self.assertIn("process-model", status)
            self.assertNotIn("file-model", status)
            self.assertNotIn("private-api", status)
            self.assertNotIn("-log", status)
        self.assertEqual(setup_env._mask("SUB_AGENT_LLM_API_KEY", process["SUB_AGENT_LLM_API_KEY"]), "****")

    def test_readme_and_current_guides_link_to_existing_local_files(self):
        for relative in ("README.md", "docs/CONFIGURATION_CHECKLIST.md", "docs/CLOUD_AUTOMATION.md",
                         "docs/PROJECT_HEALTH_REPORT.md", "docs/PARADIGM_RADAR_DESIGN.md"):
            file = ROOT / relative
            for target in re.findall(r"\]\(([^)]+)\)", file.read_text()):
                if re.match(r"\w+://", target) or target.startswith("#"):
                    continue
                target = target.split("#", 1)[0]
                self.assertTrue((file.parent / target).exists(), f"Broken local link in {relative}: {target}")


if __name__ == "__main__":
    unittest.main()
