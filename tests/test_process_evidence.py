from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.process_runner import (
    _command_requests_service_tier,
    _evaluate_service_tier_evidence,
    _extract_observed,
    _extract_session_observed,
)


class ProcessEvidenceTests(unittest.TestCase):
    def test_extracts_thread_id_from_exec_events(self) -> None:
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
            ]
        )
        models, reasoning, service_tiers, thread_ids = _extract_observed(stream)
        self.assertEqual(models, [])
        self.assertEqual(reasoning, [])
        self.assertEqual(service_tiers, [])
        self.assertEqual(thread_ids, ["thread-123"])

    def test_extracts_authoritative_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_root = Path(temporary)
            session_dir = session_root / "2026" / "07" / "13"
            session_dir.mkdir(parents=True)
            session_file = session_dir / "rollout-thread-123.jsonl"
            session_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "turn_context",
                                "payload": {"model": "gpt-5.6-sol", "effort": "max"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "thread_settings_applied",
                                    "thread_settings": {
                                        "model": "gpt-5.6-sol",
                                        "reasoning_effort": "max",
                                        "service_tier": "priority",
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            models, reasoning, service_tiers, files = _extract_session_observed(
                ["thread-123"], session_root
            )
        self.assertEqual(models, ["gpt-5.6-sol"])
        self.assertEqual(reasoning, ["max"])
        self.assertEqual(service_tiers, ["priority"])
        self.assertEqual(files, [str(session_file)])

    def test_thread_settings_tier_is_not_backend_observation(self) -> None:
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_settings_applied",
                            "thread_settings": {
                                "model": "gpt-5.6-sol",
                                "reasoning_effort": "max",
                                "service_tier": "priority",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "max",
                    }
                ),
            ]
        )
        models, reasoning, service_tiers, _ = _extract_observed(stream)
        self.assertEqual(models, ["gpt-5.6-sol"])
        self.assertEqual(reasoning, ["max"])
        self.assertEqual(service_tiers, [])

    def test_cli_without_backend_tier_is_acceptable_but_not_verified(self) -> None:
        evidence = _evaluate_service_tier_evidence(
            "priority", [], request_verified=True
        )
        self.assertEqual(evidence["service_tier_observation_status"], "not_exposed")
        self.assertTrue(evidence["service_tier_acceptable"])
        self.assertFalse(evidence["service_tier_verified"])
        self.assertFalse(evidence["fast_degraded"])

    def test_observed_fast_degradation_is_distinct_from_match(self) -> None:
        evidence = _evaluate_service_tier_evidence(
            "priority", ["default"], request_verified=True
        )
        self.assertEqual(evidence["service_tier_observation_status"], "degraded")
        self.assertTrue(evidence["service_tier_acceptable"])
        self.assertFalse(evidence["service_tier_verified"])
        self.assertTrue(evidence["fast_degraded"])

    def test_exact_cli_override_is_detected(self) -> None:
        command = ["codex", "exec", "-c", 'service_tier="priority"', "-"]
        self.assertTrue(_command_requests_service_tier(command, "priority"))
        self.assertFalse(_command_requests_service_tier(command, "default"))


if __name__ == "__main__":
    unittest.main()
