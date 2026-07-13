from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.entry_context import discover_entry_context


class EntryContextTests(unittest.TestCase):
    def test_latest_thread_settings_are_used_for_follow_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "2026" / "07" / "14" / "rollout-thread-123.jsonl"
            session.parent.mkdir(parents=True)
            events = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "medium",
                            "service_tier": "default",
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": "gpt-5.6-terra",
                            "reasoning_effort": "xhigh",
                            "service_tier": "priority",
                        },
                    },
                },
            ]
            session.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
            )
            context = discover_entry_context(thread_id="thread-123", session_root=root)
        self.assertEqual(context["model"], "gpt-5.6-terra")
        self.assertEqual(context["reasoning"], "xhigh")
        self.assertEqual(context["service_tier"], "priority")
        self.assertEqual(context["service_tier_source"], "session")

    def test_turn_context_supplies_model_and_reasoning_when_tier_is_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "2026" / "07" / "14" / "rollout-thread-456.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {
                            "model": "gpt-5.6-terra",
                            "effort": "medium",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            context = discover_entry_context(thread_id="thread-456", session_root=root)
        self.assertEqual(context["model"], "gpt-5.6-terra")
        self.assertEqual(context["reasoning"], "medium")
        self.assertEqual(context["service_tier"], "default")
        self.assertEqual(context["service_tier_source"], "default-unavailable")
        self.assertEqual(context["source"], str(session))


if __name__ == "__main__":
    unittest.main()
