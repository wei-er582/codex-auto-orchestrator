from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator.model_catalog import ModelCatalog
from orchestrator.speed_profiles import (
    ProfileStore,
    SpeedConfigurationError,
    SpeedSetupRequired,
    builtin_matrix,
    parse_matrix_text,
)


class SpeedProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Path(self.temporary.name) / "orchestrator" / "config.json"
        self.store = ProfileStore(self.config)
        self.catalog = ModelCatalog.discover(
            [sys.executable, str(ROOT / "tests" / "fake_codex.py")]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_run_requires_saved_default_before_planning(self) -> None:
        with self.assertRaises(SpeedSetupRequired) as raised:
            self.store.resolve(self.catalog)
        self.assertEqual(raised.exception.reason, "first_setup")

    def test_balanced_builtin_matches_documented_defaults(self) -> None:
        matrix = builtin_matrix("balanced", self.catalog)
        self.assertEqual(matrix["sol"]["max"], "priority")
        self.assertEqual(matrix["sol"]["ultra"], "priority")
        self.assertEqual(matrix["terra"]["ultra"], "priority")
        self.assertEqual(matrix["terra"]["max"], "default")

    def test_unicode_profile_can_be_saved_and_set_default(self) -> None:
        matrix = builtin_matrix("balanced", self.catalog)
        self.store.save_profile("日常开发", matrix, self.catalog, set_default=True)
        resolved = self.store.resolve(self.catalog)
        self.assertEqual(resolved.profile_name, "日常开发")
        self.assertEqual(resolved.tier_for(self.catalog, "gpt-5.6-sol", "max"), "priority")

    def test_follow_entry_uses_entry_tier_for_every_supported_cell(self) -> None:
        fast = builtin_matrix("follow-entry", self.catalog, "priority")
        standard = builtin_matrix("follow-entry", self.catalog, "default")
        self.assertTrue(all(tier == "priority" for family in fast.values() for tier in family.values()))
        self.assertTrue(all(tier == "default" for family in standard.values() for tier in family.values()))

    def test_new_reasoning_level_requires_profile_update(self) -> None:
        self.store.save_profile(
            "日常开发", builtin_matrix("balanced", self.catalog), self.catalog, set_default=True
        )
        payload = {
            "models": [
                {
                    "slug": info.slug,
                    "supported_reasoning_levels": [
                        *({"effort": effort} for effort in info.efforts),
                        {"effort": "super"},
                    ],
                    "service_tiers": [{"id": "priority"}],
                }
                for info in self.catalog.models.values()
            ]
        }
        changed = ModelCatalog.from_payload(payload)
        with self.assertRaises(SpeedSetupRequired) as raised:
            self.store.resolve(changed)
        self.assertEqual(raised.exception.reason, "catalog_changed")
        self.assertTrue(any(item.endswith(":super") for item in raised.exception.missing))

    def test_builtins_are_immutable_and_default_profile_cannot_be_deleted(self) -> None:
        with self.assertRaises(SpeedConfigurationError):
            self.store.save_profile("balanced", builtin_matrix("balanced", self.catalog), self.catalog)
        self.store.save_profile(
            "节约额度", builtin_matrix("all-standard", self.catalog), self.catalog, set_default=True
        )
        with self.assertRaises(SpeedConfigurationError):
            self.store.delete_profile("节约额度")

    def test_copy_rename_delete_and_default_switch(self) -> None:
        self.store.copy_profile("balanced", "日常开发", self.catalog)
        self.store.copy_profile("all-fast", "赶时间", self.catalog)
        self.store.set_default("日常开发")
        self.store.rename_profile("赶时间", "紧急任务")
        self.store.set_default("紧急任务")
        self.store.delete_profile("日常开发")
        config = self.store.read()
        self.assertEqual(config["default_profile"], "紧急任务")
        self.assertEqual(set(config["profiles"]), {"紧急任务"})

    def test_text_matrix_rejects_unknown_effort(self) -> None:
        with self.assertRaises(SpeedConfigurationError):
            parse_matrix_text("Sol Fast = max, imaginary\nTerra Fast = ultra", self.catalog)
        matrix = parse_matrix_text("Sol Fast = xhigh, max\nTerra Fast = high, ultra", self.catalog)
        self.assertEqual(matrix["sol"]["max"], "priority")
        self.assertEqual(matrix["terra"]["medium"], "default")

    def test_atomic_lock_preserves_concurrent_profile_writes(self) -> None:
        errors: list[Exception] = []

        def save(index: int) -> None:
            try:
                self.store.save_profile(
                    f"配置 {index}", builtin_matrix("all-standard", self.catalog), self.catalog
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=save, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.read()["profiles"]), 8)


if __name__ == "__main__":
    unittest.main()
