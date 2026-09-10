---
name: shunt
description: Route large, predictable, read-heavy repository work or oversized test/build output to a subscription-authenticated GPT-5.6 Luna worker, then verify its focused citations with the primary model. Use when the user invokes Codex Shunt, asks for bulk repository inspection, inventory, classification, extraction, summarization, log triage, token-routing metrics, or when a Codex Shunt hook redirects a large read.
---

# Codex Shunt

Keep architecture, ambiguous debugging, security decisions, migrations, final
review, and other judgment-heavy work in the primary model. Use the Shunt runner
for large, predictable evidence collection.

## Locate the runner

Prefer `$PLUGIN_ROOT/scripts/codex-shunt` when `PLUGIN_ROOT` is available.
Otherwise use `$HOME/plugins/codex-shunt/scripts/codex-shunt`. If neither exists,
tell the user that the local plugin source cannot be located.

## Route a read

1. Formulate one narrow question and the smallest useful set of repository paths.
2. Run:

   ```bash
   "$HOME/plugins/codex-shunt/scripts/codex-shunt" inspect \
     --root "$(git rev-parse --show-toplevel)" \
     --question "<precise evidence question>" \
     <path> [<path> ...]
   ```

3. Treat the worker response as evidence, not final judgment.
4. Reread only the cited ranges needed to verify material claims.
5. If the worker fails or asks for escalation, continue in the primary model. A
   direct read blocked by the hook can be retried once by prefixing the command
   with `CODEX_SHUNT_BYPASS=1`.

Never pass `.env` files, private keys, credentials, tokens, keychains, or other
secrets. The runner also filters common secret-bearing paths and copies approved
text files into an isolated temporary workspace before starting Luna.

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

If an in-sandbox attempt reports a read-only state database, an unavailable
metrics database, or failure to initialize the in-process app-server client,
retry Shunt once with the scoped outer approval. Do not repeat the same
in-sandbox invocation.

## Metrics

Use the runner for metrics and reports:

```bash
"$HOME/plugins/codex-shunt/scripts/codex-shunt" stats --since 7d
"$HOME/plugins/codex-shunt/scripts/codex-shunt" report --since 30d
"$HOME/plugins/codex-shunt/scripts/codex-shunt" export --since all --format json
```

Worker token counts are exact. Primary-context tokens avoided and
Sol-equivalent credits are estimates and must stay labeled as estimates.

## Routing boundaries

Good tasks: repository inventories, symbol/call-site collection, extraction,
classification, large-file summaries, test-log triage, and tightly specified
boilerplate analysis.

Do not route: architecture, security or privacy judgment, ambiguous debugging,
schema migrations, destructive work, final review, or decisions whose cost of a
wrong answer is high.
