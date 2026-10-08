---
name: setup
description: Set up Codex Shunt after installation, verify the local Luna worker, and enable automatic routing when the user requests it. Use for first-run setup, setup repair, or checking whether Shunt is ready.
---

# Set up Codex Shunt

Use the `scripts/codex-shunt` runner bundled in this plugin. Its root is two
directories above this file. Do not require a source checkout or the optional
`shunt` Terminal symlink.

On native Windows, invoke the bundled runner with
`py -3 -X utf8 "<resolved-plugin-root>/scripts/codex-shunt" <command>`.
The optional Terminal command is `shunt.cmd` on Windows.

1. Run the bundled runner's `doctor` command and report any missing Python,
   Codex executable, or ChatGPT authentication prerequisite. The initial
   source-sharing check is expected to fail until acknowledgement. Help the
   user fix other failed prerequisites, then rerun the check.
2. Explain that selected repository files will be copied into an ephemeral,
   read-only GPT-6 Luna invocation using the user's Codex login and subscription
   allowance. The worker receives those selected file contents; Shunt stores
   content-free local usage metrics. Ask the user to explicitly accept this
   source sharing before recording acknowledgement. Invoking this skill alone
   is not acceptance.
3. After explicit acceptance, run the bundled runner with
   `setup --accept-source-sharing`.
   The test uses a synthetic two-line fixture. Strict routing is enabled only
   after that worker test succeeds. If the command needs host access to Codex
   authentication/runtime services or its local metrics store, request the
   narrow permission for this runner; the Luna child remains read-only.
4. Explain that Codex requires a separate one-time review and trust decision
   for the bundled `PreToolUse` hook. Direct the user to `/hooks`
   in Codex CLI when that is their surface. Do not claim automatic interception
   is active until the hooks are trusted. If hook trust cannot be inspected from
   the current surface, say that it remains for the user to verify.
5. Run `status` and report whether setup and strict routing are ready. Suggest
   one safe, explicit repository inspection as a first use. If setup fails,
   leave strict routing off and report the failing prerequisite.

This workflow is for a local Codex task. ChatGPT web installation does not
deploy or execute the bundled hooks and local worker.
