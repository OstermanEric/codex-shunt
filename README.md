# Codex Shunt

Route large repository reads to **GPT-6 Luna** using your existing ChatGPT/Codex
login. The primary model receives a compact, cited summary and keeps responsibility
for decisions and edits.

Shunt is a local Codex plugin with a Python standard-library runtime. It needs
Python 3.11+, a Codex CLI with ChatGPT authentication, and access to GPT-6 Luna.
Supported hosts are macOS, Linux, and Windows through WSL2.

## Install

Run this once in Terminal (macOS, Linux, or WSL2):

```bash
curl -fsSL https://raw.githubusercontent.com/OstermanEric/codex-shunt/main/install.sh | bash
```

The installer downloads Shunt, installs the Git marketplace plugin, creates the
`shunt` command, and starts guided setup. It adds `~/.local/bin` to your bash/zsh
startup file when needed; reopen Terminal afterward to use `shunt`.

Setup explains source sharing, checks the existing login, sends a synthetic
two-line fixture through Luna, and enables routing only after verification.
Review and trust the plugin's hook in Codex, then start a new chat. Installed
plugin users can also invoke `$codex-shunt:setup` without a Terminal command.

Shunt prefers the desktop app's bundled CLI, then searches PATH.
To choose a specific runtime, set `CODEX_SHUNT_CODEX_PATH`. If authentication is missing,
run `codex login` with ChatGPT. Shunt uses `codex exec` and removes API-key
environment variables from its worker; no separate API key is needed.

**Distribution:** this hook-based plugin uses manual/Git marketplace installation.
Lifecycle-hook packages are currently ineligible for the public Plugins Directory.
[Official packaging guidance](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks).

## Configure

```bash
shunt config show
shunt config set min_file_lines 500
shunt config set min_source_bytes 100000
shunt config set comparison_model gpt-6.1-sol
```

You can edit `~/.local/share/codex-shunt/config.toml` directly. Partial files override
the bundled defaults:

```toml
strict_routing = true
worker_model = "gpt-6-luna"
comparison_model = "gpt-6.1-sol"
min_file_lines = 500
min_source_bytes = 100000
worker_timeout_seconds = 150
```

Routing triggers at **500 or more requested lines OR 100,000 or more requested
bytes**, by default. Thresholds apply to the supported read's text, including
bounded ranges, rather than the entire file behind every request.

Fresh installs leave routing off until setup. Use `shunt config set strict_routing
false` to disable it. Configuration also exposes source-sharing acknowledgement,
reasoning effort, and maximum source-file, source-byte, and result-size limits.
Retired compression/retention settings in older files are ignored.

## Routing

The pre-hook recognizes a deliberately small set of read-only shell commands:

- `cat` with explicit files or unquoted file globs.
- `head` / `tail` with line or byte counts, including `tail -n +N`.
- `sed -n 'N,Mp'` for one file.
- `rg` with a pattern and explicit files/globs; supported flags are
  `-n`, `-i`, `-F`, and their long forms.
- The same commands through `rtk` or `rtk proxy`.

Commands containing edits, pipelines, redirections, compound operations, shell
expansions, or unsupported flags run normally. MCP tools are not intercepted.

Selected text files are filtered and copied into a temporary workspace. Luna runs
read-only with hooks disabled and a timeout. A successful, cited result replaces
the original read; missing consent, unavailable runtime, invalid evidence, or a
worker request for escalation lets the original read proceed.

Shunt sends the selected file contents to the worker, even when the requested
operation is a range or search. The worker receives that operation as task data
and must focus its evidence accordingly. Common sensitive paths and generated
files are excluded; do not select secrets. Local telemetry stores counts and
hashes, without source text, prompts, or answers.

To inspect files explicitly:

```bash
shunt inspect --root . --question "Where is routing configured?" src
```

To obtain raw text after an unsuitable summary, retry once:

```bash
CODEX_SHUNT_BYPASS=1 cat path/to/file
```

## Stats

```bash
shunt stats --since 7d
shunt stats --since all --json
shunt stats --since 30d --compare-to gpt-6-sol
```

The default comparison is **GPT-6.1 Sol**. Change `comparison_model` to persist a
preference, or use `--compare-to` for one report. Both reprice historical token
usage; they do not change the worker model. Supported comparison models are
listed by `shunt stats --help`.

Stats include routing/fallback counts, observed worker tokens, success, citation
ranges, latency, and estimated credits. Setup checks are excluded. Failed and
escalated work adds worker cost but contributes no comparison savings.

**Savings are estimates:** successful worker tokens are repriced at the comparison
model's standard rates, then all real worker costs are subtracted. This is not an
observed account charge, measured subscription-allowance reduction, or end-to-end
savings. Source interception is a gross estimate that does not subtract replacement
summaries, verification, or retries. Citation checks validate file paths and line
ranges, not the truth of a claim.

Reports discover both Terminal and Codex plugin-data stores. Set
`CODEX_SHUNT_DATA_DIR` only to intentionally scope config and metrics to one store.
The bundled rate snapshot is dated October 8, 2026.
[Official rates](https://learn.chatgpt.com/docs/pricing#token-rates).

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

After updating an installed plugin, reinstall/refresh it and start a new chat.
Changed hook definitions require another trust review. Removed commands from
0.1 are `summarize-log`, `report`, `export`, `feedback`, and `data-dir`; use
`inspect`, `stats --json`, and `config show`. Existing metrics remain readable.

For troubleshooting, run `shunt status` or set `CODEX_SHUNT_DEBUG=1` to explain
hook fallbacks. Native PowerShell, web ChatGPT, and cloud orchestration cannot
run this local hook/worker flow.

Licensed under [MIT](LICENSE).
