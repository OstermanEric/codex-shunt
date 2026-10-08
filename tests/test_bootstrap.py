"""Exercise the one-line install with isolated HOME and fake network/model tools."""

import errno
import json
import os
try:
    import pty
except ImportError:  # Windows uses the PowerShell installer tests.
    pty = None
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(pty is None, "POSIX installer; native Windows is covered separately")
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        self.calls = self.home / 'calls.jsonl'
        self.environment = {**os.environ, 'HOME': str(self.home),
                            'PATH': f'{self.bin}:/usr/bin:/bin', 'SHELL': '/bin/zsh',
                            'TEST_SOURCE': str(ROOT), 'TEST_INSTALLER': str(ROOT / 'install.sh'),
                            'TEST_CALLS': str(self.calls),
                            'CODEX_SHUNT_CODEX_PATH': str(self.bin / 'codex')}
        for key in ('PLUGIN_ROOT', 'PLUGIN_DATA', 'CLAUDE_PLUGIN_DATA', 'ZDOTDIR',
                    'CODEX_SHUNT_DATA_DIR', 'CODEX_SHUNT_BIN_DIR', 'CODEX_SHUNT_WORKER',
                    'CODEX_SHUNT_STRICT_ROUTING', 'CODEX_SHUNT_MODE',
                    'CODEX_SHUNT_SOURCE_SHARING_ACKNOWLEDGED'):
            self.environment.pop(key, None)
        prefix = (f'#!{sys.executable}\nimport json, os, pathlib, shutil, sys\n'
                  'args = sys.argv[1:]\n'
                  'with open(os.environ["TEST_CALLS"], "a") as log:\n'
                  '    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, *args])+"\\n")\n')
        self.executable('git', prefix + '''
if args[0] == 'clone':
    destination = pathlib.Path(args[-1])
    shutil.copytree(os.environ['TEST_SOURCE'], destination,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    (destination / '.git').mkdir()
elif args[2:5] == ['remote', 'get-url', 'origin']:
    print(os.environ.get('TEST_REMOTE', 'https://github.com/OstermanEric/codex-shunt.git'))
elif args[2:4] == ['status', '--porcelain']:
    print(os.environ.get('TEST_DIRTY', ''), end='')
''')
        self.executable('codex', prefix + '''
if args[:2] == ['login', 'status']:
    print('Logged in using ChatGPT')
elif args[0] == 'exec':
    result = pathlib.Path(args[args.index('--output-last-message')+1])
    source = (result.parent / '.codex-shunt-files.txt').read_text().splitlines()[0]
    result.write_text(json.dumps({'summary': 'The safety mode is read-only.',
        'findings': [{'file': source, 'line_start': 2, 'line_end': 2,
                      'claim': 'Safety mode is read-only.', 'confidence': 1}],
        'recommended_reads': [], 'limitations': [], 'needs_escalation': False}))
    print(json.dumps({'type':'turn.completed', 'usage': {'input_tokens':100, 'output_tokens':20}}))
''')

    def executable(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_install(self, reply='accept'):
        # A real controlling tty verifies setup works when bash reads its script
        # from the pipe, as it does in the public curl command.
        pid, terminal = pty.fork()
        if pid == 0:
            os.execve('/bin/bash', ['bash', '-c', 'cat "$TEST_INSTALLER" | bash'], self.environment)
        os.write(terminal, (reply + '\n').encode())
        output = bytearray()
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                if not select.select([terminal], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(terminal, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail('Installer timed out')
        finally:
            os.close(terminal)
            _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), output.decode(errors='replace')

    def test_one_line_pipe_installs_sets_path_and_verifies_worker(self):
        profile = self.home / '.zshrc'
        profile.write_text('# Existing preferences\n')
        status, output = self.run_install()
        self.assertEqual(status, 0, output)
        checkout = self.home / '.codex/plugins/codex-shunt'
        self.assertEqual((self.home / '.local/bin/shunt').resolve(),
                         (checkout / 'scripts/codex-shunt').resolve())
        config = (self.home / '.local/share/codex-shunt/config.toml').read_text()
        self.assertIn('strict_routing = true', config)
        self.assertIn('source_sharing_acknowledged = true', config)
        self.assertTrue(profile.read_text().startswith('# Existing preferences\n'))
        self.assertIn('Review and trust', output)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertIn(['codex', 'plugin', 'add', 'codex-shunt@codex-shunt'], calls)
        self.assertTrue(any(call[:2] == ['codex', 'exec'] for call in calls))
        status, output = self.run_install()
        self.assertEqual(status, 0, output)
        self.assertEqual(profile.read_text().count('# Codex Shunt command'), 1)

    def test_declining_consent_never_starts_a_worker(self):
        status, output = self.run_install(reply='')
        self.assertNotEqual(status, 0, output)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(any(call[:2] == ['codex', 'exec'] for call in calls))
        self.assertFalse((self.home / '.local/share/codex-shunt/config.toml').exists())

    def test_noninteractive_install_defers_consent(self):
        result = subprocess.run(['/bin/bash', str(ROOT / 'install.sh')],
                                input='', text=True, capture_output=True,
                                env=self.environment, start_new_session=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('$codex-shunt:setup', result.stdout)
        self.assertFalse((self.home / '.local/share/codex-shunt/config.toml').exists())

    def test_bash_preserves_existing_login_profile(self):
        self.environment['SHELL'] = '/bin/bash'
        profile = self.home / ('.profile' if sys.platform == 'darwin' else '.bashrc')
        profile.write_text('# Existing shell preferences\n')
        status, output = self.run_install()
        self.assertEqual(status, 0, output)
        self.assertTrue(profile.read_text().startswith('# Existing shell preferences\n'))
        self.assertIn('# Codex Shunt command', profile.read_text())
        if sys.platform == 'darwin':
            self.assertFalse((self.home / '.bash_profile').exists())

    def test_existing_unmanaged_directory_is_preserved(self):
        checkout = self.home / '.codex/plugins/codex-shunt'
        checkout.mkdir(parents=True)
        marker = checkout / 'mine.txt'
        marker.write_text('keep')
        status, output = self.run_install()
        self.assertNotEqual(status, 0)
        self.assertIn('Refusing to replace', output)
        self.assertEqual(marker.read_text(), 'keep')

    def test_existing_dirty_or_unrelated_checkout_is_preserved(self):
        for setting, value in [('TEST_DIRTY', ' M README.md'), ('TEST_REMOTE', 'https://example.com/other.git')]:
            checkout = self.home / '.codex/plugins/codex-shunt'
            (checkout / '.git').mkdir(parents=True, exist_ok=True)
            self.environment[setting] = value
            status, output = self.run_install()
            self.assertNotEqual(status, 0, output)
            self.assertFalse((self.home / '.local/bin/shunt').exists())
            self.environment.pop(setting)


if __name__ == '__main__':
    unittest.main()
