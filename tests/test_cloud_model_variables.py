"""Cloud model selection is editable Variables; credentials remain Secrets."""
import os
import subprocess
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from agents.llm_utils import resolve_model


ROOT = Path(__file__).resolve().parents[1]
MODEL_FIELDS = ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL")


class CloudModelVariablesTests(unittest.TestCase):
    def workflows(self):
        for name in ("weekly-radar.yml", "one-route-acceptance.yml"):
            yield name, (ROOT / ".github/workflows" / name).read_text()

    def test_both_model_workflows_use_explicit_variables_and_secret_keys(self):
        for name, workflow in self.workflows():
            with self.subTest(workflow=name):
                for field in MODEL_FIELDS:
                    line = next(row.strip() for row in workflow.splitlines()
                                if row.strip().startswith(field + ":"))
                    self.assertEqual(line, field + ": ${{ vars." + field + " }}")
                self.assertIn("LLM_API_KEY: ${{ secrets.LLM_API_KEY || secrets.DASHSCOPE_API_KEY }}", workflow)
                self.assertNotIn("vars.LLM_API_KEY", workflow)

    def test_workflow_guards_accept_replacement_and_reject_each_missing_variable(self):
        values = dict(zip(MODEL_FIELDS, ("other-provider", "other-model", "https://offline.invalid/v1")))
        # Execute the actual workflow guard without any network or production credentials.
        for name, workflow in self.workflows():
            marker = "          for variable in LLM_PROVIDER LLM_MODEL LLM_BASE_URL; do\n"
            guard = marker + workflow.split(marker, 1)[1].split("          done\n", 1)[0] + "          done\n"
            guard = textwrap.dedent(guard)
            env = {"PATH": os.defpath, **values}
            with self.subTest(workflow=name, missing=None):
                result = subprocess.run(["bash", "-c", guard], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for field in MODEL_FIELDS:
                with self.subTest(workflow=name, missing=field):
                    result = subprocess.run(["bash", "-c", guard], env={**env, field: ""},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(field, result.stdout)
                    self.assertIn("Variables", result.stdout)
        weekly = (ROOT / ".github/workflows/weekly-radar.yml").read_text()
        doctor = weekly.split("      - name: 配置体检\n", 1)[1].split("      - name:", 1)[0]
        self.assertIn("if: ${{ !inputs.source_check_only }}", doctor)

    def test_both_roles_inherit_all_replacement_fields(self):
        fields = {"LLM_PROVIDER": "other-provider", "LLM_MODEL": "other-model",
                  "LLM_BASE_URL": "https://offline.invalid/v1", "LLM_API_KEY": "offline-key"}
        fields.update({role + "_" + field: "" for role in ("SUB_AGENT", "MAIN_AGENT")
                       for field in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY")})
        with patch.multiple(config, **fields):
            for role in ("sub", "main"):
                model = resolve_model(role)
                self.assertEqual((model.provider, model.model, model.base_url, model.api_key),
                                 tuple(fields[field] for field in (*MODEL_FIELDS, "LLM_API_KEY")))
                self.assertEqual(model.model_from, "全局LLM配置")


if __name__ == "__main__":
    unittest.main()
