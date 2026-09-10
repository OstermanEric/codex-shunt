import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PLUGIN_ROOT / "scripts" / "install-command"
RUNNER = PLUGIN_ROOT / "scripts" / "codex-shunt"


class InstallCommandTests(unittest.TestCase):
    def run_installer(self, bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["CODEX_SHUNT_BIN_DIR"] = str(bin_dir)
        environment["PATH"] = "/usr/bin:/bin"
        return subprocess.run(
            [str(INSTALLER), *args],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_install_is_idempotent_and_points_to_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"

            first = self.run_installer(bin_dir)
            second = self.run_installer(bin_dir)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((bin_dir / "shunt").resolve(), RUNNER.resolve())
            self.assertIn("Add this directory to PATH", first.stdout)

    def test_install_refuses_unmanaged_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            command_path = bin_dir / "shunt"
            command_path.write_text("user command\n", encoding="utf-8")

            result = self.run_installer(bin_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to overwrite", result.stderr)
            self.assertEqual(command_path.read_text(encoding="utf-8"), "user command\n")

    def test_uninstall_only_removes_managed_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            self.assertEqual(self.run_installer(bin_dir).returncode, 0)

            result = self.run_installer(bin_dir, "--uninstall")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((bin_dir / "shunt").exists())


if __name__ == "__main__":
    unittest.main()
