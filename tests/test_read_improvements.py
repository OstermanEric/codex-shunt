"""Compatibility and evidence boundaries for context searches and read pipelines."""

import io
import base64
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
from codex_shunt import hooks, worker
from codex_shunt.cli import _render_stats
from codex_shunt.config import load_config
from codex_shunt.db import aggregate, fetch_rows
from codex_shunt.reads import (parse_read, read_metrics, recognize_read, requested_line_ranges,
                              search_statement_indices)
from codex_shunt.sources import collect_sources


class ReadFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ['a.txt', 'b.txt']:
            (self.root / name).write_text(''.join(f'{name} line {n}\n' for n in range(1, 601)), encoding='utf-8')

    def plan(self, command, shell='powershell'):
        read = parse_read(command, self.root, 500, shell)
        self.assertIsNotNone(read)
        selection = collect_sources(self.root, [str(p) for p in read.paths], max_files=500, max_bytes=1000000)
        return read, selection


class ReadImprovements(ReadFixture):
    def test_chained_selection_preserves_stage_order_clipping_and_file_boundaries(self):
        cases = [
            ('Get-Content a.txt,b.txt | Select-Object -Skip 590 | Select-Object -First 30',
             [('a.txt', 591, 600), ('b.txt', 1, 20)], 30),
            ('Get-Content a.txt,b.txt | Select-Object -First 610 | Select-Object -Skip 590',
             [('a.txt', 591, 600), ('b.txt', 1, 10)], 20),
            ('Get-Content a.txt | Select-Object -First 100 | Select-Object -Skip 200', [], 0),
            ('Get-Content a.txt | Select-Object -Skip 100 | Select-Object -First 0', [], 0),
            ('Get-Content a.txt,a.txt -Tail 20 | Select-Object -Skip 10 | Select-Object -First 20',
             [('a.txt', 591, 600), ('a.txt', 581, 590)], 20),
            ('Get-Content a.txt,b.txt -TotalCount 100 | select -Skip 50 -First 120 | select -Skip 60 | select -First 20',
             [('b.txt', 11, 30)], 20),
        ]
        for command, ranges, count in cases:
            with self.subTest(command=command):
                read, selection = self.plan(command)
                self.assertEqual(requested_line_ranges(read, selection), [ranges])
                self.assertEqual(read_metrics(read, selection)['source_lines'], count)
                self.assertIn('chained-line-slice', read.features)
        for suffix in ['| Select-Object -Last 3', '| Select-Object -Skip -1', '| Write-Output label',
                       '| Select-Object -Index 1', '| Select-Object -First 1 -First 2']:
            self.assertIsNone(parse_read('Get-Content a.txt | Select-Object -Skip 1 ' + suffix,
                                         self.root, 500, 'powershell'))
        command = 'Get-Content a.txt' + ' | select -Skip 1' * 17
        self.assertEqual(recognize_read(command, self.root, 500, 'powershell').reason, 'pipeline-limit')

    @unittest.skipUnless(os.name == 'nt', 'requires native Windows PowerShell')
    def test_native_chained_selections_match_resolved_source_lines(self):
        for engine in ['powershell', 'pwsh']:
            if not shutil.which(engine):
                continue
            for options in [
                '| Select-Object -Skip 590 | Select-Object -First 30',
                '| Select-Object -First 610 | Select-Object -Skip 590',
                '| Select-Object -First 10 | Select-Object -Skip 20',
                '| Select-Object -Skip 580 -First 80 | Select-Object -Skip 10 -First 20',
            ]:
                with self.subTest(engine=engine, options=options):
                    command = 'Get-Content a.txt,b.txt -Encoding UTF8 ' + options
                    read, selection = self.plan(command, engine)
                    actual = subprocess.run([shutil.which(engine), '-NoProfile', '-NonInteractive',
                                             '-Command', command], cwd=self.root, capture_output=True, check=True)
                    expected = []
                    for filename, start, end in requested_line_ranges(read, selection)[0]:
                        expected.extend((self.root / filename).read_text().splitlines()[start - 1:end])
                    self.assertEqual(actual.stdout.decode('ascii').splitlines(), expected)
                    self.assertEqual(read_metrics(read, selection)['source_lines'], len(expected))

    def test_rejection_metadata_is_bounded_and_survives_into_stats(self):
        cases = [
            ('rg --max-count=SECRET line a.txt', 'rejected-option:rg:max-count', 'bash'),
            ('rg --SECRET=TOKEN line a.txt', 'rejected-option:rg:other', 'bash'),
            ('Get-Content a.txt -Delimiter SECRET', 'rejected-option:get-content:delimiter', 'powershell'),
            ('Get-Content a.txt | select -Last 5', 'rejected-option:select-object:last', 'powershell'),
            ('Get-Content a.txt -Raw | select -First 5', 'pipeline:raw-line-selection', 'powershell'),
            ('Get-Content a.txt; Write-Output SECRET', 'batch', 'powershell'),
        ]
        environment = {'CODEX_SHUNT_DATA_DIR': str(self.root / 'metrics'), 'CODEX_SHUNT_WORKER': '0'}
        with patch.dict(os.environ, environment):
            for command, feature, shell in cases:
                recognition = recognize_read(command, self.root, 500, shell)
                self.assertIsNone(recognition.read)
                self.assertIn(feature, recognition.features)
                event = {'tool_name': 'Bash', 'cwd': str(self.root),
                         'tool_input': {'cmd': command, 'shell': shell}}
                with patch.object(hooks, '_read_event', return_value=event), \
                        patch.object(hooks, 'run_worker') as run, redirect_stdout(io.StringIO()):
                    hooks.hook_pre()
                    run.assert_not_called()
            rows = fetch_rows('routing_events', 'all')
            stored = json.dumps(rows)
            for private in ['SECRET', 'TOKEN', str(self.root), 'a.txt', 'Write-Output']:
                self.assertNotIn(private, stored)
            coverage = aggregate('all')['coverage']
            tags = {item['feature'] for item in coverage['by_feature']}
            self.assertTrue({feature for _, feature, _ in cases} <= tags)
            self.assertIsNone(coverage['fallback_rate'])
            for width in [20, 40, 80]:
                self.assertTrue(all(len(line) <= width for line in
                                    _render_stats(aggregate('all'), 'all', width=width).splitlines()))


@unittest.skipUnless(shutil.which('rg'), 'ripgrep is unavailable')
class ContextSearchTests(ReadFixture):
    def setUp(self):
        super().setUp()
        # Close matches overlap, while the final hit leaves an unselected gap.
        self.name = 'unicode café:scope.txt' if os.name != 'nt' else 'unicode café scope.txt'
        self.lines = ['needle' if n in {4, 6, 18} else f'line {n}' for n in range(1, 21)]
        (self.root / self.name).write_bytes(('\r\n'.join(self.lines)).encode('utf-8'))

    def context_plan(self, options):
        return self.plan(f"rg -n {options} needle '{self.name}'", 'bash')

    def result(self, *ranges):
        return {'summary': 'Matching source', 'needs_escalation': False, 'limitations': [],
                'recommended_reads': [], 'findings': [
                    {'file': filename, 'line_start': start, 'line_end': end,
                     'claim': 'Match', 'confidence': 1.0} for filename, start, end in ranges]}

    def test_context_option_forms_native_output_and_overlapping_spans(self):
        for options in ['-C 2', '-C2', '--context=2', '--context 2',
                        '-A2 -B2', '--after-context=2 --before-context 2']:
            with self.subTest(options=options):
                read, selection = self.context_plan(options)
                native = subprocess.run([shutil.which('rg'), '--no-config', '--color=never',
                                         *read.operation['flags'], '--', 'needle',
                                         *(str(p) for p in read.paths)], cwd=self.root, capture_output=True, check=True)
                metrics = read_metrics(read, selection)
                self.assertEqual(metrics['source_bytes'], len(native.stdout))
                self.assertEqual(metrics['source_lines'], len(native.stdout.splitlines()))
                self.assertEqual(requested_line_ranges(read, selection),
                                 [[(self.name, 2, 8), (self.name, 16, 20)]])
                self.assertIn('context-search', read.features)
        for options, expected in [('-C0', [(self.name, 4, 4), (self.name, 6, 6), (self.name, 18, 18)]),
                                  ('-A1', [(self.name, 4, 7), (self.name, 18, 19)]),
                                  ('-B1', [(self.name, 3, 6), (self.name, 17, 18)])]:
            read, selection = self.context_plan(options)
            self.assertEqual(requested_line_ranges(read, selection), [expected])
        read, selection = self.plan("rg needle --context=2 -g '*.txt' .", 'bash')
        self.assertEqual(requested_line_ranges(read, selection), [[(self.name, 2, 8), (self.name, 16, 20)]])
        for option in ['-C -1', '--context=bad', '-C2147483648', '-C', '--context=', '-C-3']:
            self.assertIsNone(parse_read(f"rg {option} needle '{self.name}'", self.root, 500, 'bash'))

    def test_search_citations_reject_gaps_and_other_files_without_requiring_every_hit(self):
        read, selection = self.context_plan('-C2')
        ranges = requested_line_ranges(read, selection)
        for span in [(self.name, 9, 9), (self.name, 8, 16), ('a.txt', 4, 4)]:
            with self.subTest(span=span), self.assertRaises(worker.WorkerError):
                worker._validate_result(self.result(span), self.root, 10000,
                                        {self.name, 'a.txt'}, ranges, search_statement_indices(read))
        self.assertEqual(worker._validate_result(self.result((self.name, 4, 4)), self.root, 10000,
                                                {self.name}, ranges, (0,)), (1, 1))
        for command in [f"rg -C2 needle '{self.name}'; cat a.txt",
                        f"rg -C2 needle '{self.name}'; rg 'line 400' b.txt"]:
            read, selection = self.plan(command, 'bash')
            ranges = requested_line_ranges(read, selection)
            with self.assertRaisesRegex(worker.WorkerError, 'omitted evidence'):
                worker._validate_result(self.result((self.name, 4, 4)), self.root, 10000,
                                        {self.name, 'a.txt', 'b.txt'}, ranges, search_statement_indices(read))

    def test_empty_search_and_unscoped_batch_do_not_widen_search_citations(self):
        read, selection = self.plan(f"rg absent '{self.name}'", 'bash')
        self.assertEqual(requested_line_ranges(read, selection), [[]])
        read, selection = self.plan(f"rg -C2 needle '{self.name}'; head -c 10 a.txt", 'bash')
        with self.assertRaisesRegex(ValueError, 'search-scope-unavailable'):
            requested_line_ranges(read, selection)

    def test_scope_limits_and_invalid_structured_output_fail_open(self):
        read, selection = self.context_plan('-C2')
        event = {'type': 'match', 'data': {'path': {'text': str(self.root / 'outside.txt')}, 'line_number': 1}}
        with patch('codex_shunt.reads.run_read_process', return_value=json.dumps(event).encode()):
            with self.assertRaisesRegex(ValueError, 'search-evaluation-failed'):
                requested_line_ranges(read, selection)
        event['data']['path'] = {'bytes': base64.b64encode(os.fsencode(self.root / self.name)).decode('ascii')}
        with patch('codex_shunt.reads.run_read_process', return_value=json.dumps(event).encode()):
            self.assertEqual(requested_line_ranges(read, selection), [[(self.name, 1, 1)]])
        event['data']['line_number'] = True
        with patch('codex_shunt.reads.run_read_process', return_value=json.dumps(event).encode()):
            with self.assertRaisesRegex(ValueError, 'search-evaluation-failed'):
                requested_line_ranges(read, selection)
        (self.root / 'many.txt').write_text('needle\nother\n' * 513)
        read, selection = self.plan('rg needle many.txt', 'bash')
        with self.assertRaisesRegex(ValueError, 'search-scope-limit'):
            requested_line_ranges(read, selection)

    def test_hook_passes_scopes_and_records_replaced_tokens_or_whole_call_fallback(self):
        environment = {'CODEX_SHUNT_DATA_DIR': str(self.root / 'metrics'), 'CODEX_SHUNT_WORKER': '0'}
        config = load_config()
        config.update(strict_routing=True, source_sharing_acknowledged=True, min_file_lines=1)
        outcome = SimpleNamespace(run_id='context', citation_count=1, valid_citation_count=1,
                                  result=self.result((self.name, 4, 4)))
        event = {'tool_name': 'Bash', 'cwd': str(self.root), 'tool_input': {
            'cmd': f"rg -C2 needle '{self.name}'", 'shell': 'bash'}}
        with patch.dict(os.environ, environment), patch.object(hooks, '_read_event', return_value=event), \
                patch.object(hooks, 'load_config', return_value=config), \
                patch.object(hooks, 'run_worker', return_value=outcome) as run:
            output = io.StringIO()
            with redirect_stdout(output):
                hooks.hook_pre()
            self.assertIn('permissionDecision', output.getvalue())
            self.assertEqual(run.call_args.kwargs['requested_ranges'],
                             [[(self.name, 2, 8), (self.name, 16, 20)]])
            self.assertEqual(run.call_args.kwargs['search_statements'], (0,))
            run.reset_mock()
            event['tool_input']['cmd'] += '; head -c 10 a.txt'
            output = io.StringIO()
            with redirect_stdout(output):
                hooks.hook_pre()
            self.assertEqual(output.getvalue(), '')
            run.assert_not_called()
            payload = aggregate('all')
            coverage = payload['coverage']
            self.assertEqual(coverage['fallbacks'], 1)
            self.assertEqual(coverage['fallback_rate'], .5)
            self.assertGreater(coverage['estimated_routed_tokens'], 0)
            self.assertEqual(coverage['estimated_routed_tokens'], payload['routing']['estimated_source_tokens_intercepted'])
            context = next(item for item in coverage['by_feature'] if item['feature'] == 'context-search')
            self.assertEqual(context['estimated_routed_tokens'], coverage['estimated_routed_tokens'])


if __name__ == '__main__':
    unittest.main()
