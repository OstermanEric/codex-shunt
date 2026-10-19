# User Report Investigation — October 19, 2026

Investigated against source checkout `8195449` (0.3.0). The reporter confirmed
the affected version was 0.3.0 and supplied the workdir command. The fixes below
are included in **0.3.1**. The actual hook
event and worker JSON remain unavailable, so device-specific conclusions are
distinguished from source evidence and local reproductions.

## Pipeline Answer Used Characters Instead Of Lines

The parser and requested-output measurement interpret `Get-Content file |
Select-Object -Skip 50 -First 500` as 500 line objects, lines 51–550 for a file
with at least 550 lines. The worker previously received the normalized
`slice: {skip: 50, first: 500}` operation but no explicit slice unit or resolved
source ranges. Validation checked that citation paths and line bounds existed,
not whether they matched the operation. A citation could therefore be structurally
valid even when the worker misunderstood the operation.

This is a reported model interpretation error plus a confirmed Shunt validation
gap. Shunt now calculates line ranges locally, includes explicit units and ranges
in the question and returned hook context, rejects out-of-selection citations,
and requires evidence for each nonempty selected range in line-only batches.
Byte reads and searches retain their existing operation representation and
file/line validation. These structural checks cannot prove semantic correctness
or full reading: a wrong summary with an in-range citation can still pass.

## Multiline Escalation

Recognizing both reads and launching one worker demonstrates recognition and
an attempted route. An escalating result deliberately allows the entire original
shell call. It records a routing fallback; it is not a successful interception.
No fallback-policy change is needed. A regression now checks this exact two-read
case, its ordered ranges, one worker invocation, and `route_failed` telemetry.
Without the worker limitations, the reason for that particular escalation is unknown.

## Working Directory Mismatch

The supplied call was `Get-Content -LiteralPath 'fixture-a.txt'` with a separate
absolute `C:/Users/testuser/onedrive/shunt-synthetic` workdir (the pasted command
omitted its closing quote; actual successful execution implies a transcription
typo). It returned 600 lines but the hook recorded `missing-or-nonfile-path`.

The current Shunt hook already handles a supplied workdir correctly. However,
[Codex's exec pre-hook projection](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs#L478-L487)
forwards only `{command: args.cmd}`, while execution separately resolves workdir.
[Official hook documentation](https://learn.chatgpt.com/docs/hooks#common-input-fields)
defines `cwd` as the session directory. [Upstream issue #32360](https://github.com/openai/codex/issues/32360)
reports the same loss. This is a strong explanation for the device report; the
actual installed Codex build/payload is still needed for definitive attribution.

A local reproduction passes the identical relative command through a command-only
hook payload and gets the reported missing-path skip; preserving workdir makes
it eligible. Absolute/relative sibling and relative child workdir tests also pass.
Shunt cannot reconstruct omitted metadata safely; it does not scan transcripts
or guess another directory. Use the intended session project root or explicit
`inspect --root`. A same-named file in the session root could instead be mistaken
for the requested source, so relative reads with a hidden override remain a
correctness limitation of the upstream contract.

Opt-in debug output previously said only “unsupported operation.” It now includes
the specific skip category and effective working directory, plus an omitted-workdir
hint for missing paths. Diagnostics remain stderr-only, not telemetry.

## Invisible Metrics Stores

Confirmed reporting bug: discovery used `glob`/`is_file`, suppressed filesystem
errors, and readonly connection/query failures were silently ignored. An empty
merged result displayed zero activity without saying data might be inaccessible.

Reporting now uses explicit directory enumeration and stat calls, retains
categorized discovery/open/query issues, and provides an availability envelope
for both normal and failed-run reports. No readable store means `unavailable`;
readable stores plus an issue means `partial`. A successfully read empty database
remains a valid zero-activity report. Explicit data-dir overrides stay isolated,
readonly reporting never creates a store, and accessible rows are still deduplicated.
JSON consumers must check availability before interpreting aggregate zeros.
No discovery mechanism can identify a store whose existence is entirely hidden
without an observable error; host access remains the check for sandbox visibility.

## Live Validation

A real subscription-authenticated Luna run on a synthetic 600-line file succeeded
with the patched range contract: it summarized all selected sections at 51–200,
201–400 and 401–550 with three valid citations and no escalation. This establishes
one successful live test, not a reliability guarantee or affected-device acceptance.

The full local suite ran 136 tests: 128 passed and eight platform-only checks were
skipped on macOS. A real readonly metrics comparison also exercised the fix:
sandboxed reporting marked its two readable stores partial when a third could
not open; host access read all three and reported complete data. Compilation and
diff-whitespace checks passed. The affected Windows device still needs to verify
the updated plugin and its installed Codex hook payload.
