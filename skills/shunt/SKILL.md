---
name: shunt
description: Route large, predictable, read-heavy repository work or oversized test/build output to a subscription-authenticated GPT-6 Luna worker, then verify its focused citations with the primary model. Use when the user invokes Codex Shunt, asks for bulk repository inspection, inventory, classification, extraction, summarization, log triage, token-routing metrics, or when a Codex Shunt hook redirects a large read.
---

# Codex Shunt

Keep architecture, ambiguous debugging, security decisions, migrations, final
review, and other judgment-heavy work in the primary model. Use the Shunt runner
for large, predictable evidence collection.

## Locate the runner

Use the executable bundled in the same plugin as this skill. Resolve the plugin
root two directories above this `SKILL.md`, then use
`<plugin-root>/scripts/codex-shunt`. This works from Codex's versioned installed
snapshot and never requires a generated cache path, symlink, or source checkout.

When `PLUGIN_ROOT` is already available, use
`$PLUGIN_ROOT/scripts/codex-shunt`. The optional `shunt` command on `PATH` and
`$HOME/plugins/codex-shunt/scripts/codex-shunt` source checkout are fallbacks for
ordinary Terminal and local development. If no bundled runner exists, continue
in the primary model and tell the user the plugin installation is incomplete.

## Route a read

1. Formulate one narrow question and the smallest useful set of repository paths.
2. Before the first worker run, check `shunt status`. If source sharing is not
   acknowledged, explain that the filtered contents of the selected files will
   be sent to a separate, ephemeral, read-only Luna invocation using the same
   ChatGPT/Codex login and subscription allowance. Then run `shunt setup`
   interactively, or use `shunt setup --accept-source-sharing` only after the
   user has explicitly accepted that disclosure.
3. Run:

   ```bash
   "<resolved-plugin-root>/scripts/codex-shunt" inspect \
     --root "$(git rev-parse --show-toplevel)" \
     --question "<precise evidence question>" \
     <path> [<path> ...]
   ```

4. Treat the worker response as evidence, not final judgment.
5. Reread only the cited ranges needed to verify material claims.
6. If the worker fails or asks for escalation, continue in the primary model. A
   direct read blocked by the hook can be retried once by prefixing the command
   with `CODEX_SHUNT_BYPASS=1`.

Never pass `.env` files, private keys, credentials, tokens, keychains, or other
secrets. The runner also filters common secret-bearing paths and copies approved
text files into an isolated temporary workspace before starting Luna.

## Strict routing

Strict routing is off by default. When it is on, the bundled `PreToolUse` hook
runs the same filtered, read-only Luna worker itself and returns its compact
result before denying the original broad read. It must fail open: if the runner,
configuration, telemetry, authentication, or worker fails, allow the original
tool call.

The hook accepts both the legacy Bash `tool_input.command` field and the
`tool_input.cmd` field used by current code-mode `exec_command` calls. Codex
still exposes both forms to hook matchers as `Bash`.

When the user asks to enable or disable strict routing, run:

```bash
"<resolved-plugin-root>/scripts/codex-shunt" config set strict_routing true
"<resolved-plugin-root>/scripts/codex-shunt" config set strict_routing false
```

Enabling strict routing requires the source-sharing acknowledgement. For a
first-time setup, prefer:

```bash
"<resolved-plugin-root>/scripts/codex-shunt" setup \
  --accept-source-sharing --enable-strict-routing
```

`--enable-strict-routing` first sends a synthetic two-line fixture through Luna
and persists strict mode only if that end-to-end check succeeds.

Do not ask the user to clear plugin caches. A legacy `mode = "enforce"` setting
is migrated automatically.

## Run from a sandboxed Codex task

When invoking the runner through a local Codex task's shell, request narrowly
scoped elevated/unsandboxed execution for the Shunt command on the first call.
Use a reusable prefix limited to the resolved runner plus its `inspect` or
`summarize-log` subcommand when the host supports persistent approvals. The
outer permission is required because a nested `codex exec` must access Codex's
local authentication/runtime services and Shunt's metrics database.

This does not relax the Luna worker: Shunt copies filtered sources into a
temporary workspace and launches the child with `--sandbox read-only`. Never
replace that child policy with `--dangerously-bypass-approvals-and-sandbox`.

The approval justification must disclose both facts: which selected repository
paths will be sent to the separate Luna invocation, and that the outer launcher
needs host access only for Codex authentication/runtime services and local
metrics. Do not describe this merely as a local read or omit the model transfer.

If an in-sandbox attempt reports a read-only state database, an unavailable
metrics database, or failure to initialize the in-process app-server client,
retry Shunt once with the scoped outer approval. Do not repeat the same
in-sandbox invocation.

## Metrics

Use the runner for metrics and reports:

```bash
"<resolved-plugin-root>/scripts/codex-shunt" stats --since 7d
"<resolved-plugin-root>/scripts/codex-shunt" report --since 30d
"<resolved-plugin-root>/scripts/codex-shunt" export --since all --format json
```

Worker token counts are exact. Primary-context tokens avoided and
Sol-equivalent credits are estimates and must stay labeled as estimates.
Stats, reports, and exports automatically discover the terminal store and
Codex plugin-data stores; use `CODEX_SHUNT_DATA_DIR` only when intentionally
scoping a report to one metrics database.

## Routing boundaries

Good tasks: repository inventories, symbol/call-site collection, extraction,
classification, large-file summaries, test-log triage, and tightly specified
boilerplate analysis.

Do not route: architecture, security or privacy judgment, ambiguous debugging,
schema migrations, destructive work, final review, or decisions whose cost of a
wrong answer is high.
