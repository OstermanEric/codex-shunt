from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.config import get_data_dir, load_config, save_user_config


class ConfigTests(unittest.TestCase):
    def test_strict_routing_defaults_on_and_preserves_saved_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}, clear=True
        ):
            config = load_config()
            self.assertTrue(config["strict_routing"])
            self.assertFalse(config["source_sharing_acknowledged"])
            config["strict_routing"] = False
            save_user_config(config)
            self.assertFalse(load_config()["strict_routing"])

    def test_legacy_off_and_shadow_preserve_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}, clear=True
        ):
            for mode in ("off", "shadow"):
                with self.subTest(mode=mode):
                    (Path(temporary) / "config.toml").write_text(f'mode = "{mode}"\n')
                    self.assertFalse(load_config()["strict_routing"])

    def test_comparison_default_persists_and_rejects_unknown_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}, clear=False
        ):
            config = load_config()
            self.assertEqual(config["comparison_model"], "gpt-6.1-sol")
            config["comparison_model"] = "gpt-6-sol"
            save_user_config(config)
            self.assertEqual(load_config()["comparison_model"], "gpt-6-sol")
            config["comparison_model"] = "unknown-model"
            with self.assertRaisesRegex(ValueError, "comparison_model"):
                save_user_config(config)

    def test_retired_settings_do_not_break_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}, clear=False
        ):
            (Path(temporary) / "config.toml").write_text(
                'post_tool_compression = true\npost_tool_min_bytes = 100000\n'
                'retain_raw_worker_events = false\nworker_model = "gpt-5.6-luna"\n'
            )
            config = load_config()
            self.assertNotIn("post_tool_compression", config)
            self.assertEqual(config["worker_model"], "gpt-5.6-luna")

    def test_source_sharing_requires_explicit_acknowledgement_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ, {"CODEX_SHUNT_DATA_DIR": temporary}, clear=False
            ):
                config = load_config()
            self.assertFalse(config["source_sharing_acknowledged"])

    def test_source_sharing_environment_override_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "CODEX_SHUNT_DATA_DIR": temporary,
                "CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED": "true",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertTrue(load_config()["source_sharing_acknowledged"])

    def test_plugin_data_is_default_writable_location_for_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin_data = Path(temporary) / "plugin-data"
            environment = {
                "PLUGIN_DATA": str(plugin_data),
            }
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(get_data_dir(), plugin_data.resolve())

    def test_legacy_enforce_mode_migrates_without_cache_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            data_dir.mkdir(exist_ok=True)
            (data_dir / "config.toml").write_text('mode = "enforce"\n', encoding="utf-8")
            with patch.dict(
                os.environ, {"CODEX_SHUNT_DATA_DIR": str(data_dir)}, clear=False
            ):
                config = load_config()
            self.assertTrue(config["strict_routing"])
            self.assertNotIn("mode", config)


if __name__ == "__main__":
    unittest.main()
