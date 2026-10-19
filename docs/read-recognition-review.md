# Read Recognition Review

Date: October 19, 2026. Source checkout: `f57d425`.

This is the pre-implementation investigation. Version 0.3.0 implements literal
arrays, native PowerShell wildcards and constrained searches, UTF-8 parameters,
line slices, ordered read batches, directory/glob ripgrep searches, and coverage
telemetry. See the current README for supported syntax and remaining limits.

The reported skips are reproducible. Most are intentional limitations of the
current read grammar. Extending common declarative reads is worthwhile;
automatically replacing arbitrary scripts is not a practical extension of this
design. This review makes recommendations, not implementation changes.

## Evidence and Scope

A temporary diagnostic exercised 33 commands against real 600-line fixture
files, including both `examle.html` and `example.html`. It called the parser and
hook, mocking the worker, consent/configuration, and telemetry. Four commands
were recognized: three qualifying controls reached the mocked worker, and a
400-line control correctly remained below threshold. The other 29 commands
were rejected by recognition, including all the reported unsupported categories
and a mixed read/write control.

The 25 existing tests in `RoutingRegressions`, `WindowsReadTests`, and
`PreHookTests` pass. Reproduce that existing-test check with:

```sh
rtk proxy env PYTHONPATH=src:tests python3 -m unittest \
  test_regressions.RoutingRegressions \
  test_windows.WindowsReadTests \
  test_hooks.PreHookTests -v
```

These checks ran on macOS with the recognizer's platform set to Windows, as in
the existing Windows parser tests. No PowerShell executable is installed here.
They establish this checkout's recognition/hook behavior, not actual execution
semantics on the other device or its installed plugin version.

## Findings by Command Family

| Form | Current reason for skipping | Recommendation |
| --- | --- | --- |
| `Get-Content test.htm, examle.html` | The tokenizer rejects unquoted commas on Windows; the PowerShell reader also permits only one path. Quoted array elements are unsupported too. | High value, modest effort: support literal comma-separated path arrays, including `-Path` and `-LiteralPath`, with quote-aware parsing. |
| `Get-Content *.html` | The PowerShell reader explicitly rejects wildcard paths, including quoted ones. POSIX unquoted `cat` globs already work. | High value, moderate effort: add bounded FileSystem wildcard resolution with PowerShell semantics. |
| `Get-Content test.html -Encoding UTF8` | Only `-Path`, `-LiteralPath`, `-TotalCount`, `-Tail`, and `-Raw` are allowed. | High value, small initial scope: accept explicit UTF-8 variants for validated UTF-8 input; model decoding rather than discarding the flag. |
| `Get-Content test.html \| Select-Object -Skip 100 -First 400` | All unquoted pipe operators are rejected before command-specific parsing. | High value, moderate effort: whitelist this specific line-slicing composition. |
| Two `Get-Content` commands separated by a newline | Any newline or carriage return rejects the whole command. Semicolon-separated reads are also rejected. | High value, moderate effort: parse an ordered batch and replace it only when every statement is supported. |
| `Select-String -Pattern needle -Path test.html`, `sls ...` | Neither command is recognized. | Worth adding, moderate effort: begin with literal paths and a narrow set of search options. |
| `rg needle .`, `rg -n needle src`, `rg needle` | Every supplied operand must resolve to a file; directories fail. No explicit path also fails. | High value, moderate effort: implement bounded recursive content-search selection and implicit current-directory roots. |
| `rg -g '*.html' needle .`, `rg needle -g '*.html' .` | Only `-n`, `-i`, `-F`, and long equivalents are supported, before the pattern. Directory operands fail separately. | High value: implement option arguments, normal option placement, and directory selection together. |
| `rg --glob='*.html' needle .` | It also fails the tokenizer's rejection of adjacent quoted/unquoted token fragments. | Include common attached option forms in the tokenizer work. |
| `Get-ChildItem ...`, `dir`, `ls`, `rg --files` | These inventory operations have no normalized representation in the hook. | Lower priority alone; distinguish filename inventories from content searches. |
| `Get-ChildItem ... \| Select-String ...` | Both an unsupported pipeline and unsupported command families. | Worth supporting after enumeration and search primitives exist. |
| `Get-Content $file`, interpolation, `$(...)` | Dollar signs and other evaluation syntax are rejected globally. | Defer general evaluation. A small later extension could resolve literal local assignments within the same invocation. |
| Arbitrary scripts, loops, Python/PowerShell programs | Only a short command allowlist exists; the hook cannot establish their complete behavior. | Keep automatic replacement unsupported; use explicit `inspect` for known source paths and a focused question. |

The `.htm`/`.html` extensions are not the problem. A single explicit text file
with either extension can be routed. Expanding only the punctuation allowlist
would not fix the underlying missing operation semantics.

## Important Behavioral Details

**Recognition and routing eligibility are separate.** Defaults are 500 requested
lines OR 100,000 requested bytes. A recognized 400-line slice normally should
run directly unless its returned text reaches the byte threshold. The example
slice means source lines 101–500 for an ordinary single-file, line-producing
`Get-Content`. `-Raw` changes its output to one string and must not use the same
slice interpretation. See Microsoft's [Select-Object documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/select-object?view=powershell-7.5).

**The hook replaces the entire shell call.** It denies the original command only
after a successful worker result. Recognizing just the first read in a mixed
script would suppress its remaining commands. Batches therefore need complete
validation, ordered operation data, and an all-or-nothing fallback. File copies
may be deduplicated, but requested operations, repeated output, and per-file
versus pipeline-wide ranges must retain their meaning.

**The Windows parser was intentionally narrow.** The README describes these
limits, and `test_shell_evaluation_wildcards_and_unsupported_flags_stay_raw`
explicitly expects encoding flags, wildcards, arrays, and pipelines to bypass
routing. Expanding support means updating that contract and its regression tests.

**Directory support exists below the recognizer.** `collect_sources` already
walks directories, filters files, and enforces budgets, but `parse_read` does
not admit directory search roots. Reusing that walker alone would not preserve
`rg` selection: ignore files, hidden-file rules, ordered include/exclude globs,
and explicit paths matter. Prefer ripgrep's bounded enumeration with the same
supported filters, then apply Shunt's source policy. If exclusions could change
the intended answer, let the original command run. Ripgrep documents glob
precedence in its [official guide](https://github.com/BurntSushi/ripgrep/blob/master/GUIDE.md#manual-filtering-globs).

**Encoding support extends beyond parsing.** Metrics currently slice raw bytes;
the text detector rejects NUL-containing samples, including typical UTF-16 text.
Full encoding support needs decoded text, consistent line counting, worker
copies, and original-source citation mapping. Explicit UTF-8 is a useful first
step. Windows PowerShell and modern PowerShell have different encoding defaults;
see Microsoft's [encoding documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-7.5)
and [Get-Content parameters](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/get-content?view=powershell-7.5#-encoding).

**Select-String needs its own search semantics.** It defaults to case-insensitive
regular-expression matching and produces MatchInfo objects. It is not equivalent
to the current default `rg` operation. Start with a constrained literal-search
form or an explicitly modeled PowerShell matcher; do not silently substitute
another regex engine. See [Select-String](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/select-string?view=powershell-7.5).

**Inventories should not cause content uploads.** A filename listing needs an
inventory result and metrics based on the listing. The current worker/hook
expects findings with source line citations and copies selected file contents.
Routing a directory listing through that unchanged contract adds unnecessary
work and disclosure. Small inventories are unlikely to justify worker latency.

**Shell identity is currently inferred from the host OS.** `sys.platform` selects
PowerShell behavior; the parser receives no shell argument. Prefer explicit shell
metadata when available, with a conservative fallback. PowerShell on macOS and
Bash on Windows need distinct parsing despite the host platform.

## Suggested Implementation Order

1. Add structured skip reasons and aggregate counts for likely read commands.
   Unsupported syntax currently returns before routing telemetry and emits only
   a generic optional debug message. Existing stats cannot quantify this gap or
   support an honest savings estimate. Store reason categories, not raw commands
   or expanded variable values.
2. Introduce shell-specific tokenization feeding a normalized ordered read plan.
   Add literal file arrays and explicit UTF-8 support first, then bounded
   FileSystem wildcard resolution. Preserve quotes and distinguish separators
   from characters inside literal filenames.
3. Add the specific `Get-Content | Select-Object -Skip N -First M` composition and
   batches containing only recognized reads. Use final requested-output metrics
   and one aggregate worker call. Preserve full-call fallback on unsupported
   statements or unsuccessful evidence.
4. Expand `rg` to directory/implicit roots and `-g`/`--glob` argument forms,
   including options after the pattern, attached values, and repeated globs.
   Bound enumeration before expensive file reads or output capture. Keep
   execution-related flags such as `--pre` unsupported.
5. Add constrained `Select-String` and FileSystem enumeration-to-search pipelines.
   Consider standalone inventories separately, after measuring their frequency.
6. Revisit literal local assignments only if usage data justifies them. Continue
   to leave arbitrary expressions, session-dependent variables, and scripts on
   the primary path.

For broader PowerShell syntax, evaluate its native
[Parser.ParseInput API](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.parser.parseinput?view=powershellsdk-7.4.0)
as a syntax frontend returning an AST. Parsing must not execute the command.
An AST does not itself prove that a command is a supported read. Whitelist node
and operation types, account for startup cost and constrained-language hosts,
and fail open if the parsing helper is unavailable. A small quote-aware grammar
remains reasonable for the initial limited forms; avoid growing one regex into
a general shell interpreter.

Acceptance should include real Windows PowerShell/PowerShell 7 comparisons,
quoted commas and wildcard characters, CRLF and multiline input, range/Raw
differences, duplicate and overlapping reads, Unicode, zero matches, missing
files, ignore/glob precedence, provider/alternate-stream rejection, symlinks and
root containment, source limits, mixed read/write calls, and worker fallback.
Keep validation proportional to each added feature.

## Code Map

- `src/codex_shunt/reads.py:23`: tokenizer, operator rejection, host-based dispatch.
- `src/codex_shunt/reads.py:96`: supported `rg` flags and explicit path handling.
- `src/codex_shunt/reads.py:138`: single-file PowerShell grammar.
- `src/codex_shunt/reads.py:192`: requested-output metrics.
- `src/codex_shunt/hooks.py:50`: whole-call interception and fallback.
- `src/codex_shunt/hooks.py:73`: selection completeness check.
- `src/codex_shunt/hooks.py:80`: line/byte thresholds.
- `src/codex_shunt/sources.py:72`: text/encoding heuristic.
- `src/codex_shunt/sources.py:121`: directory-capable source collection.
- `tests/test_windows.py:50`: deliberate unsupported-form expectations.
- `tests/test_regressions.py:54`: mixed/compound-command fallback expectations.
- `README.md:156`: documented recognition contract.
