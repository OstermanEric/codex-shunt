# README assets

- `logo.svg` is the shared Codex Shunt logo used by the plugin and README.
- `stats.png` is a 2× terminal-style rendering of the actual ANSI output from
  `python3 scripts/codex-shunt stats --since all --color always`, captured on
  October 8, 2026 at an 80-column width. The frame uses the README's indigo palette.
  It preserves every metric, including historical failures and escalations.

The stats image is a development-history snapshot, not a benchmark. When
refreshing it, capture the complete output without changing numbers, update the
date in the README, and keep the estimate/accounting caveats visible. Do not
include local paths, run identifiers, chat identifiers, or source contents.

The routing diagram lives directly in the README as Mermaid so changes to the
flow can be reviewed as text. Navigation badges use Shields.io; the Tests badge
links to the existing GitHub Actions workflow.
