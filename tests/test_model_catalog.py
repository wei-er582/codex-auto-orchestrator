from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.model_catalog import ModelCatalog
from orchestrator.schemas import ID_PATTERN, ValidationError, validate_plan
from orchestrator.util import uses_chatgpt_login


class ModelCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = [sys.executable, str(ROOT / "tests" / "fake_codex.py")]

    def test_discovers_sol_terra_and_ultra(self) -> None:
        catalog = ModelCatalog.discover(self.fake)
        self.assertEqual(catalog.preferred_sol(), "gpt-5.6-sol")
        self.assertEqual(catalog.preferred_terra(), "gpt-5.6-terra")
        self.assertIn("ultra", catalog.models["gpt-5.6-sol"].efforts)
        self.assertTrue(uses_chatgpt_login(self.fake))

    def test_prefers_latest_matching_model_without_hardcoding_version(self) -> None:
        levels = [{"effort": effort} for effort in ("medium", "max")]
        catalog = ModelCatalog.from_payload(
            {
                "models": [
                    {"slug": "gpt-5.6-sol", "supported_reasoning_levels": levels},
                    {"slug": "gpt-5.7-sol", "supported_reasoning_levels": levels},
                ]
            }
        )
        self.assertEqual(catalog.preferred_sol(), "gpt-5.7-sol")

    def test_rejects_ultra_inside_orchestrated_mode(self) -> None:
        catalog = ModelCatalog.discover(self.fake)
        worker = {
            "id": "worker",
            "title": "worker",
            "objective": "work",
            "model": "gpt-5.6-sol",
            "reasoning": "ultra",
            "depends_on": [],
            "access": "read",
            "allowed_paths": [],
            "acceptance": ["done"],
            "timeout_seconds": 120,
        }
        plan = {
            "version": 1,
            "summary": "invalid",
            "complexity": "S3",
            "risk": "medium",
            "execution_mode": "orchestrated",
            "rationale": "invalid nested Ultra",
            "waves": [{"id": "wave", "tasks": [worker, worker | {"id": "worker-2"}]}],
            "final_review": {"required": True, "model": "gpt-5.6-sol", "reasoning": "max", "acceptance": []},
            "permissions": {"commit": False, "push": False, "deploy": False, "external_write": False},
        }
        with self.assertRaises(ValidationError):
            validate_plan(plan, catalog, 3)

    def test_rejects_same_wave_dependency_and_concurrency_overflow(self) -> None:
        catalog = ModelCatalog.discover(self.fake)
        first = _worker("first")
        second = _worker("second") | {"depends_on": ["first"]}
        same_wave = _plan([first, second])
        with self.assertRaises(ValidationError):
            validate_plan(same_wave, catalog, 3)

        too_many = _plan([_worker(f"worker-{index}") for index in range(4)])
        with self.assertRaises(ValidationError):
            validate_plan(too_many, catalog, 3)

    def test_required_review_is_forced_to_sol_max(self) -> None:
        catalog = ModelCatalog.discover(self.fake)
        plan = _plan([_worker("first"), _worker("second")])
        plan["final_review"] = {
            "required": True,
            "model": "gpt-5.6-terra",
            "reasoning": "medium",
            "acceptance": ["verify"],
        }
        validated = validate_plan(plan, catalog, 3)
        self.assertEqual(validated["final_review"]["model"], "gpt-5.6-sol")
        self.assertEqual(validated["final_review"]["reasoning"], "max")

    def test_lowercase_underscore_identifiers_are_valid(self) -> None:
        catalog = ModelCatalog.discover(self.fake)
        plan = _plan([_worker("tests_audit"), _worker("docs_audit")])
        plan["waves"][0]["id"] = "wave_1"
        validated = validate_plan(plan, catalog, 3)
        self.assertEqual(validated["waves"][0]["id"], "wave_1")

    def test_output_schemas_share_the_runtime_identifier_pattern(self) -> None:
        plan_schema = json.loads(
            (ROOT / "scripts" / "schemas" / "plan.schema.json").read_text(encoding="utf-8")
        )
        result_schema = json.loads(
            (ROOT / "scripts" / "schemas" / "result.schema.json").read_text(encoding="utf-8")
        )
        review_schema = json.loads(
            (ROOT / "scripts" / "schemas" / "review.schema.json").read_text(encoding="utf-8")
        )
        wave_properties = plan_schema["properties"]["waves"]["items"]["properties"]
        task_properties = wave_properties["tasks"]["items"]["properties"]
        assessment_properties = review_schema["properties"]["task_assessments"]["items"]["properties"]
        self.assertEqual(wave_properties["id"]["pattern"], ID_PATTERN)
        self.assertEqual(task_properties["id"]["pattern"], ID_PATTERN)
        self.assertEqual(task_properties["depends_on"]["items"]["pattern"], ID_PATTERN)
        self.assertEqual(result_schema["properties"]["task_id"]["pattern"], ID_PATTERN)
        self.assertEqual(assessment_properties["task_id"]["pattern"], ID_PATTERN)


def _worker(task_id: str) -> dict:
    return {
        "id": task_id,
        "title": task_id,
        "objective": "inspect",
        "model": "gpt-5.6-terra",
        "reasoning": "medium",
        "depends_on": [],
        "access": "read",
        "allowed_paths": [],
        "acceptance": ["done"],
        "timeout_seconds": 120,
    }


def _plan(tasks: list[dict]) -> dict:
    return {
        "version": 1,
        "summary": "test plan",
        "complexity": "S3",
        "risk": "medium",
        "execution_mode": "orchestrated",
        "rationale": "test validation",
        "waves": [{"id": "wave", "tasks": tasks}],
        "final_review": {
            "required": True,
            "model": "gpt-5.6-sol",
            "reasoning": "max",
            "acceptance": ["verify"],
        },
        "permissions": {
            "commit": False,
            "push": False,
            "deploy": False,
            "external_write": False,
        },
    }


if __name__ == "__main__":
    unittest.main()
