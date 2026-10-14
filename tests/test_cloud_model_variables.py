"""Cloud model selection is editable Variables; credentials remain Secrets."""
import os
import subprocess
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from agents.llm_utils import build_client, resolve_model


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("SUB_AGENT", "MAIN_AGENT")
MODEL_FIELDS = tuple(role + "_" + field for role in ROLES
                     for field in ("PROVIDER", "MODEL", "BASE_URL"))


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
                for role in ROLES:
                    key = role + "_LLM_API_KEY"
                    self.assertIn(key + ": ${{ secrets." + key + " }}", workflow)
                    self.assertNotIn("vars." + key, workflow)
                for old in ("vars.LLM_PROVIDER", "vars.LLM_MODEL", "vars.LLM_BASE_URL",
                            "secrets.LLM_API_KEY", "secrets.DASHSCOPE_API_KEY",
                            "secrets.SUB_AGENT_API_KEY", "secrets.MAIN_AGENT_API_KEY"):
                    self.assertNotIn(old, workflow)

    def test_workflow_guards_accept_replacement_and_reject_each_missing_variable(self):
        values = {role + "_" + field: value for role in ROLES for field, value in
                  zip(("PROVIDER", "MODEL", "BASE_URL"),
                      ("other-provider", "other-model", "https://offline.invalid/v1"))}
        # Execute the actual workflow guard without any network or production credentials.
        for name, workflow in self.workflows():
            marker = "          for variable in " + " ".join(MODEL_FIELDS) + "; do\n"
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

    def test_both_roles_use_their_own_provider_model_url_and_key(self):
        fields = {role + "_" + field: value for role in ROLES for field, value in
                  zip(("PROVIDER", "MODEL", "BASE_URL", "LLM_API_KEY"),
                      (role.lower(), role.lower() + "-model", "https://" + role.lower() + ".invalid/v1", role + "-offline-key"))}
        with patch.multiple(config, **fields):
            for role in ("sub", "main"):
                model = resolve_model(role)
                prefix = role.upper() + "_AGENT"
                self.assertEqual((model.provider, model.model, model.base_url, model.api_key),
                                 tuple(fields[prefix + "_" + field] for field in ("PROVIDER", "MODEL", "BASE_URL", "LLM_API_KEY")))
                self.assertEqual(model.model_from, "Agent专属配置")

    def test_missing_role_cannot_borrow_global_other_role_or_provider_defaults(self):
        obsolete = {"LLM_PROVIDER": "dashscope", "LLM_MODEL": "old-model",
                    "LLM_BASE_URL": "https://old.invalid/v1", "LLM_API_KEY": "old-offline-key"}
        for role in ("sub", "main"):
            prefix = role.upper() + "_AGENT"
            for field, attr in (("PROVIDER", "provider"), ("MODEL", "model"),
                                ("BASE_URL", "base_url"), ("LLM_API_KEY", "api_key")):
                with self.subTest(role=role, field=field), patch.multiple(config, create=True, **obsolete), \
                        patch.object(config, prefix + "_" + field, ""):
                    self.assertEqual(getattr(resolve_model(role), attr), "")
                    with patch("agents.llm_utils.AsyncOpenAI") as sdk:
                        with self.assertRaisesRegex(ValueError, prefix + "_" + field):
                            build_client(role)
                        sdk.assert_not_called()

    def test_ollama_only_supplies_dummy_key_not_model_or_endpoint(self):
        with patch.multiple(config, SUB_AGENT_PROVIDER="ollama", SUB_AGENT_MODEL="",
                            SUB_AGENT_BASE_URL="", SUB_AGENT_LLM_API_KEY=""):
            model = resolve_model("sub")
            self.assertEqual((model.model, model.base_url, model.api_key), ("", "", "ollama"))

    def test_new_secret_names_are_redacted_in_logs_audit_and_email(self):
        import main
        import run_audit
        from notifications.email_notifier import _safe_public_text
        with patch.multiple(config, SUB_AGENT_LLM_API_KEY="private-sub-offline-key",
                            MAIN_AGENT_LLM_API_KEY="private-main-offline-key"):
            for clean in (main._redacted_log_text, lambda text: run_audit._clean(text, 500), _safe_public_text):
                result = clean("private-sub-offline-key private-main-offline-key")
                self.assertNotIn("private-sub", result)
                self.assertNotIn("private-main", result)


if __name__ == "__main__":
    unittest.main()
