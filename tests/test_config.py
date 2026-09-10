from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.config import get_data_dir, load_config


class ConfigTests(unittest.TestCase):
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
