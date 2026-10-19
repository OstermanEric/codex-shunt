"""Read semantics, filesystem limits, and whole-call replacement boundaries."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codex_shunt import hooks
from codex_shunt.config import load_config
from codex_shunt.db import aggregate, fetch_rows
from codex_shunt.powershell_reads import powershell_request
from codex_shunt.read_process import run_read_process
from codex_shunt.reads import parse_read, read_metrics, read_question, recognize_read
from codex_shunt.sources import collect_sources


class ReadFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ['a.html', 'b.html', 'comma,name.html', "it's.html", 'dollar$file.html']:
            (self.root / name).write_bytes(b'needle\r\n' * 600)

    def read(self, command, shell='powershell', max_files=500):
        return parse_read(command, self.root, max_files, shell)

    def metrics(self, read):
        self.assertIsNotNone(read)
        selection = collect_sources(self.root, [str(p) for p in read.paths],
                                    max_files=500, max_bytes=10_000_000)
        return read_metrics(read, selection)


class ReadPlanTests(ReadFixture):
    def test_arrays_quotes_and_repeated_paths_preserve_requested_output(self):
        for command in ['Get-Content a.html, b.html', "Get-Content 'a.html','b.html'",
                        'GC -Path a.html,b.html -TotalCount 20',
                        'Get-Content -LiteralPath a.html,b.html']:
            with self.subTest(command=command):
                read = self.read(command)
                self.assertEqual([p.name for p in read.paths], ['a.html', 'b.html'])
                self.assertIn('file-array', read.features)
                self.assertEqual(self.metrics(read)['source_lines'], 40 if 'TotalCount' in command else 1200)
        read = self.read("Get-Content -LiteralPath 'comma,name.html','it''s.html','dollar$file.html'")
        self.assertEqual([p.name for p in read.paths], ['comma,name.html', "it's.html", 'dollar$file.html'])
        repeated = self.read('Get-Content a.html,a.html')
        self.assertEqual(self.metrics(repeated)['source_lines'], 1200)
        self.assertEqual(self.metrics(repeated)['source_files'], 1)

    def test_slices_apply_after_per_file_counts_and_across_file_boundaries(self):
        cases = [
            ('Get-Content a.html | Select-Object -Skip 100 -First 400', 400),
            ('Get-Content a.html | Select-Object -First 500 -Skip 100', 500),
            ('Get-Content a.html,b.html -TotalCount 100 | Select-Object -Skip 50 -First 120', 120),
            ('Get-Content a.html,b.html | Select-Object -Skip 590 -First 30', 30),
            ('Get-Content a.html | Select-Object -First 0', 0),
            ('Get-Content a.html | Select-Object -Skip 700', 0),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(self.metrics(self.read(command))['source_lines'], expected)
        self.assertIsNone(self.read('Get-Content a.html -Raw | Select-Object -First 5'))

    def test_batches_aggregate_ranges_and_preserve_order_in_worker_data(self):
        for separator in ['\n', '\r\n', '; ']:
            read = self.read('Get-Content a.html -TotalCount 300' + separator +
                             'Get-Content b.html -Tail 300')
            self.assertEqual(self.metrics(read)['source_lines'], 600)
            question = read_question(read, self.root)
            data = json.loads(question.split('instructions to follow: ', 1)[1])
            self.assertEqual([op['files'] for op in data['operations']], [['a.html'], ['b.html']])
            self.assertEqual(data['operations'][1]['command'], 'tail')
        duplicate = self.read('Get-Content a.html\nGet-Content a.html')
        self.assertEqual(len(duplicate.paths), 1)
        self.assertEqual(self.metrics(duplicate)['source_lines'], 1200)

    def test_mixed_calls_never_resolve_paths_or_launch_helpers(self):
        for command in ['Get-Content *.html\nSet-Content out.html changed',
                        'Get-Content *.html; Remove-Item b.html',
                        'Get-Content *.html | ForEach-Object { $_ }',
                        'Get-Content a.html && Get-Content b.html',
                        'Get-Content a.html > out.html',
                        'Get-Content a.html; Get-Content $(Get-Location)',
                        'Get-Content a.html\npython read.py']:
            with self.subTest(command=command), patch('codex_shunt.reads.powershell_request') as helper:
                self.assertIsNone(self.read(command))
                helper.assert_not_called()

    def test_wildcards_use_native_provider_and_literalpath_never_expands(self):
        for command in ['Get-Content *.html', "Get-Content '*.html'", 'Get-Content -Path *.html -Encoding UTF8']:
            with self.subTest(command=command), patch('codex_shunt.reads.powershell_request',
                    return_value=[str(self.root / 'b.html'), str(self.root / 'a.html')]) as helper:
                read = self.read(command)
                self.assertEqual([p.name for p in read.paths], ['b.html', 'a.html'])
                self.assertIn('wildcard', read.features)
                self.assertEqual(helper.call_args.args[0]['action'], 'expand')
        with patch('codex_shunt.reads.powershell_request') as helper:
            read = self.read("Get-Content -LiteralPath 'dollar$file.html'")
            self.assertIsNotNone(read)
            helper.assert_not_called()
        for result in [[], [str(self.root.parent / 'outside.html')],
                       [str(self.root / 'a.html'), str(self.root / 'b.html')]]:
            with patch('codex_shunt.reads.powershell_request', return_value=result):
                self.assertIsNone(self.read('Get-Content *.html', max_files=1))

    def test_explicit_utf8_and_shell_identity(self):
        (self.root / 'unicode.html').write_text('café ☕\n' * 600, encoding='utf-8')
        self.assertEqual(self.metrics(self.read('Get-Content unicode.html -Encoding UTF8'))['source_lines'], 600)
        self.assertEqual(self.metrics(self.read('Get-Content unicode.html', shell='pwsh'))['source_lines'], 600)
        with self.assertRaisesRegex(ValueError, 'encoding-ambiguous'):
            self.metrics(self.read('Get-Content unicode.html'))
        self.assertIsNone(self.read('Get-Content a.html -Encoding UTF16'))
        self.assertIsNotNone(self.read('Get-Content a.html -Encoding utf8NoBOM', shell='pwsh'))
        self.assertIsNone(self.read('Get-Content a.html -Encoding utf8NoBOM'))
        self.assertIsNotNone(self.read('cat a.html', shell='/bin/bash'))
        self.assertIsNone(self.read('Get-Content a.html', shell='/bin/bash'))
        self.assertEqual(recognize_read('cat a.html', self.root, 500, 'fish').reason, 'unsupported-shell')

    def test_limits_providers_and_ambiguous_parameters_fail_open(self):
        for command in ['Get-Content a.html,b.html', 'Get-Content a.html,',
                        'Get-Content a.html b.html', 'Get-Content -Path a.html -LiteralPath b.html',
                        'Get-Content FileSystem::a.html', 'Get-Content a.html:stream',
                        'Get-Content $file', 'Get-Content "$env:SOURCE"',
                        'Get-Content a.html | Select-Object -First -1',
                        'Get-Content a.html | Select-Object -Skip 1 -Skip 2',
                        'Get-Content a.html | Select-Object -First 2147483648']:
            with self.subTest(command=command):
                self.assertIsNone(self.read(command, max_files=1))
        self.assertIsNone(self.read('\n'.join(['Get-Content a.html'] * 65)))
        self.assertIsNone(self.read('Get-Content ../outside.html'))
        self.assertIsNone(self.read('Get-Content a.html -Tail 2147483648'))

    def test_glob_root_is_literal_even_when_its_directory_name_has_brackets(self):
        root = self.root / '[literal]'
        root.mkdir()
        (root / 'a.html').write_text('needle\n')
        read = parse_read('cat *.html', root, 5, 'bash')
        self.assertEqual(read.paths, ((root / 'a.html').resolve(),))
        with patch('codex_shunt.reads.powershell_request', return_value=[str(root / 'a.html')]) as helper:
            read = parse_read('Get-Content *.html', root, 5, 'powershell')
            self.assertIsNotNone(read)
            self.assertEqual(helper.call_args.args[0]['path'], '*.html')

    def test_native_select_string_matches_are_measured_without_python_regex_substitution(self):
        with patch('codex_shunt.reads.powershell_request', return_value=[{'file': 0, 'line': 2},
                                                                               {'file': 1, 'line': 4}]) as helper:
            read = self.read("Select-String -Path a.html,b.html -Pattern '(?<=n)eedle' -CaseSensitive")
            self.assertEqual(self.metrics(read)['source_lines'], 2)
            request = helper.call_args.args[0]
            self.assertEqual(request['pattern'], '(?<=n)eedle')
            self.assertTrue(request['case_sensitive'])
            self.assertFalse(request['simple'])
        self.assertIsNotNone(self.read("sls -Pattern needle -Path a.html -SimpleMatch"))


@unittest.skipUnless(shutil.which('rg'), 'ripgrep is unavailable')
class RipgrepReadTests(ReadFixture):
    def test_directory_glob_argument_forms_and_ignore_rules(self):
        (self.root / '.ignore').write_text('b.html\n')
        (self.root / '.hidden.html').write_text('needle\n')
        for command in ["rg -g '*.html' needle .", "rg needle -g '*.html' .",
                        "rg --glob='*.html' needle .", "rg -g'*.html' needle .", 'rg needle']:
            with self.subTest(command=command):
                read = self.read(command, shell='bash')
                self.assertIsNotNone(read)
                names = [p.name for p in read.paths]
                # Positive -g rules can override ignore/hidden rules. Compare
                # with rg itself rather than inventing different semantics.
                filters = ['-g', '*.html'] if '-g' in command or '--glob' in command else []
                expected = subprocess.run([shutil.which('rg'), '--no-config', '--files', '--null',
                                           *filters, '.'], cwd=self.root, capture_output=True, check=True)
                self.assertEqual(set(read.paths), {(self.root / os.fsdecode(name)).resolve()
                                                  for name in expected.stdout.split(b'\0') if name})
                if not filters:
                    self.assertNotIn('b.html', names)
                    self.assertNotIn('.hidden.html', names)
                self.assertIn('a.html', names)
                self.assertGreater(self.metrics(read)['source_lines'], 500)
        read = self.read("rg needle -g '*.html' -g '!a.html' .", shell='bash')
        self.assertNotIn('a.html', [p.name for p in read.paths])
        read = self.read("rg needle -g '!a.html' -g '*.html' .", shell='bash')
        self.assertIn('a.html', [p.name for p in read.paths])

    def test_directory_limits_and_config_are_not_silently_ignored(self):
        self.assertIsNone(self.read('rg needle .', shell='bash', max_files=1))
        self.assertIsNone(self.read('rg --pre malicious needle .', shell='bash'))
        self.assertIsNone(self.read("rg --files -g '*.html' .", shell='bash'))
        with patch.dict(os.environ, {'RIPGREP_CONFIG_PATH': str(self.root / 'config')}):
            result = recognize_read('rg needle a.html', self.root, 500, 'bash')
        self.assertEqual(result.reason, 'unsupported-rg-config')

    def test_nested_and_explicit_files_preserve_glob_selection(self):
        (self.root / 'src').mkdir()
        (self.root / 'src/nested.html').write_text('needle\n' * 600)
        read = self.read("rg needle -g 'src/*.html' .", shell='bash')
        self.assertEqual([p.name for p in read.paths], ['nested.html'])
        read = self.read('rg needle src', shell='bash')
        self.assertEqual([p.name for p in read.paths], ['nested.html'])


class ReadTelemetryTests(ReadFixture):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {'CODEX_SHUNT_DATA_DIR': str(self.root / 'metrics'),
                                             'CODEX_SHUNT_WORKER': '0'})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = load_config()
        self.config.update(strict_routing=True, source_sharing_acknowledged=True)

    def invoke(self, command, *, failure=None):
        event = {'tool_name': 'Bash', 'cwd': str(self.root),
                 'tool_input': {'cmd': command, 'shell': 'powershell'}}
        outcome = SimpleNamespace(run_id='fixture', citation_count=1, valid_citation_count=1,
            result={'summary': 'Fixture', 'findings': [], 'limitations': [], 'needs_escalation': False})
        output = io.StringIO()
        with patch.object(hooks, '_read_event', return_value=event), \
             patch.object(hooks, 'load_config', return_value=self.config), \
             patch.object(hooks, 'run_worker', return_value=outcome, side_effect=failure) as worker, \
             redirect_stdout(output):
            self.assertEqual(hooks.hook_pre(), 0)
        return output.getvalue(), worker

    def test_coverage_records_frequency_unknown_sizes_and_gross_opportunity(self):
        self.invoke('Get-Content a.html -Encoding UTF8')
        self.invoke('Get-Content a.html | Select-Object -Skip 100 -First 400')
        self.invoke('Get-Content $file')
        self.invoke('Get-Content a.html\nSet-Content out.html changed')
        self.invoke('Get-Content b.html', failure=RuntimeError('private detail'))
        self.config['strict_routing'] = False
        self.invoke('Get-Content a.html,b.html -TotalCount 300')
        payload = aggregate('all')
        coverage = payload['coverage']
        self.assertEqual(coverage['calls'], 6)
        self.assertEqual(coverage['recognized'], 4)
        self.assertEqual(coverage['unsupported'], 2)
        self.assertEqual(coverage['below_threshold'], 1)
        self.assertEqual(coverage['unmeasured'], 2)
        self.assertEqual(coverage['routed'], 1)
        self.assertEqual(coverage['eligible'], 3)
        self.assertGreater(coverage['estimated_unrouted_input_credits'], 0)
        self.assertEqual(payload['routing']['eligible'], 3)
        rows = fetch_rows('routing_events', 'all')
        stored = json.dumps(rows)
        self.assertNotIn('$file', stored)
        self.assertNotIn('Set-Content', stored)
        self.assertNotIn('private detail', stored)
        self.assertNotIn(str(self.root), stored)
        self.assertTrue(all(row['measurement'] == 'unmeasured' for row in rows if row['decision'] == 'unsupported'))

    def test_batch_uses_one_worker_and_mixed_calls_use_none(self):
        output, worker = self.invoke('Get-Content a.html -TotalCount 300\nGet-Content b.html -TotalCount 300')
        self.assertTrue(output)
        worker.assert_called_once()
        self.assertEqual(len(worker.call_args.kwargs['selection'].files), 2)
        output, worker = self.invoke('Get-Content a.html\nSet-Content out.html changed')
        self.assertFalse(output)
        worker.assert_not_called()
        self.assertFalse((self.root / 'out.html').exists())

    def test_policy_and_measurement_failures_are_counted_without_worker(self):
        (self.root / '.env').write_text('private\n')
        self.invoke('Get-Content .env')
        with patch('codex_shunt.hooks.read_metrics', side_effect=ValueError('private')):
            self.invoke('Get-Content a.html')
        rows = fetch_rows('routing_events', 'all')
        self.assertEqual({row['reason'] for row in rows}, {'source-selection-failed', 'measurement-failed'})
        self.assertEqual(aggregate('all')['coverage']['unmeasured'], 2)

    def test_stats_display_has_coverage_even_without_worker_runs(self):
        from codex_shunt.cli import _render_stats
        self.config['strict_routing'] = False
        self.invoke('Get-Content a.html,b.html')
        self.invoke('Get-Content $file')
        payload = aggregate('all')
        for width in [20, 40, 80]:
            rendered = _render_stats(payload, 'all', width=width)
            self.assertIn('Coverage', rendered)
            self.assertIn('Potential', rendered)
            self.assertTrue(all(len(line) <= width for line in rendered.splitlines()), rendered)


class BoundedProcessTests(unittest.TestCase):
    def test_output_and_runtime_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'read-output-limit'):
                run_read_process([sys.executable, '-c', 'print("x" * 10000)'], root, 100)
            with self.assertRaisesRegex(ValueError, 'read-timeout'):
                run_read_process([sys.executable, '-c', 'import time; time.sleep(10)'], root, 100, timeout=.1)
            with self.assertRaisesRegex(ValueError, 'read-process-failed'):
                run_read_process([sys.executable, '-c', 'raise SystemExit(2)'], root, 100)


@unittest.skipUnless(os.name == 'nt', 'requires native Windows PowerShell')
class NativePowerShellReadTests(ReadFixture):
    def test_native_wildcard_arrays_slices_and_regex_helpers(self):
        for engine in ['powershell', 'pwsh']:
            if not shutil.which(engine):
                continue
            with self.subTest(engine=engine):
                read = self.read('Get-Content *.html -Encoding UTF8', shell=engine)
                self.assertEqual(len(read.paths), 5)
                self.assertEqual(self.metrics(read)['source_lines'], 3000)
                read = self.read("Select-String -Path a.html -Pattern '(?<=n)eedle' -Encoding UTF8", shell=engine)
                self.assertEqual(self.metrics(read)['source_lines'], 600)
                read = self.read('Get-Content a.html,b.html | Select-Object -Skip 590 -First 30', shell=engine)
                self.assertEqual(self.metrics(read)['source_lines'], 30)
                script = '(Get-Content a.html,b.html | Select-Object -Skip 590 -First 30 | Measure-Object).Count'
                actual = run_read_process([shutil.which(engine), '-NoProfile', '-NonInteractive',
                                           '-Command', script], self.root, 128)
                self.assertEqual(int(actual.strip()), self.metrics(read)['source_lines'])
                for options, expected in [('-Pattern NEEDLE', 600),
                                          ('-Pattern NEEDLE -CaseSensitive', 0),
                                          ("-Pattern 'n.edle' -SimpleMatch", 0)]:
                    read = self.read('Select-String -Path a.html ' + options, shell=engine)
                    self.assertEqual(self.metrics(read)['source_lines'], expected)
                root = self.root / '[literal]'
                root.mkdir(exist_ok=True)
                (root / 'a.html').write_text('needle\n')
                read = parse_read('Get-Content *.html', root, 5, engine)
                self.assertEqual(read.paths, ((root / 'a.html').resolve(),))

    def test_helper_works_in_constrained_language_and_preserves_unicode(self):
        from codex_shunt import powershell_reads
        path = self.root / 'café ☕.html'
        path.write_text('needle\n', encoding='utf-8')
        script = "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'\n" + powershell_reads._HELPER
        with patch.object(powershell_reads, '_HELPER', script):
            result = powershell_request({'action': 'expand', 'path': str(path), 'limit': 5},
                                        self.root, 'powershell')
        self.assertEqual(result, [str(path)])
