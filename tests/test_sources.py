from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.sources import collect_sources


class SourceSelectionTests(unittest.TestCase):
    def test_collects_text_and_skips_sensitive_and_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "package.js").write_text("ignored\n", encoding="utf-8")

            selection = collect_sources(root, ["."], max_files=10, max_bytes=10000)

            self.assertEqual([item.relative.as_posix() for item in selection.files], ["src/app.py"])
            self.assertGreaterEqual(selection.excluded_files, 1)

    def test_rejects_explicit_sensitive_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("SECRET=value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sensitive"):
                collect_sources(root, [".env"], max_files=10, max_bytes=10000)

    def test_rejects_path_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            outside = Path(second) / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes"):
                collect_sources(Path(first), [str(outside)], max_files=10, max_bytes=10000)


if __name__ == "__main__":
    unittest.main()
