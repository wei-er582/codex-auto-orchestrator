from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.process_runner import _extract_observed, _extract_session_observed


class ProcessEvidenceTests(unittest.TestCase):
    def test_extracts_thread_id_from_exec_events(self) -> None:
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
            ]
        )
        models, reasoning, thread_ids = _extract_observed(stream)
        self.assertEqual(models, [])
        self.assertEqual(reasoning, [])
        self.assertEqual(thread_ids, ["thread-123"])

    def test_extracts_authoritative_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_root = Path(temporary)
            session_dir = session_root / "2026" / "07" / "13"
            session_dir.mkdir(parents=True)
            session_file = session_dir / "rollout-thread-123.jsonl"
            session_file.write_text(
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol", "effort": "max"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            models, reasoning, files = _extract_session_observed(
                ["thread-123"], session_root
            )
        self.assertEqual(models, ["gpt-5.6-sol"])
        self.assertEqual(reasoning, ["max"])
        self.assertEqual(files, [str(session_file)])


if __name__ == "__main__":
    unittest.main()
