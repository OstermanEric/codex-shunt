from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from codex_shunt.cli import _color_enabled, _render_stats, build_parser


class StatsRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "routing": {
                "total": 0,
                "routed": 0,
                "enforced": 0,
                "estimated_tokens": 0,
                "estimated_intercepted_tokens": 0,
            },
            "workers": {
                "total": 3,
                "succeeded": 2,
                "input_tokens": 154823,
                "cached_input_tokens": 94976,
                "output_tokens": 2801,
                "worker_credits": 0.4308,
                "sol_credits": 8.335,
                "average_duration_ms": 22170,
                "citations": 4,
                "valid_citations": 4,
                "escalations": 1,
            },
            "derived": {
                "routing_share": 0.0,
                "enforced_share": 0.0,
                "worker_success_rate": 2 / 3,
                "citation_validity_rate": 1.0,
                "worker_stage_credit_difference": 7.9042,
                "worker_stage_credit_savings_rate": 7.9042 / 8.335,
            },
            "by_model": [
                {
                    "worker_model": "gpt-5.6-luna",
                    "runs": 3,
                    "tokens": 157884,
                    "credits": 0.4308,
                }
            ],
        }

    def test_plain_stats_lead_with_savings_and_quality(self) -> None:
        rendered = _render_stats(self.payload, "all", color=False)
        self.assertNotIn("\033[", rendered)
        self.assertIn("Codex Shunt  ·  all time", rendered)
        self.assertIn("CREDITS SAVED      7.9042 credits", rendered)
        self.assertIn("SUCCESS RATE       66.7%", rendered)
        self.assertIn("CITATION VALIDITY  100.0%", rendered)
        self.assertIn("3 total · 2 succeeded · 1 failed · 1 escalation", rendered)
        self.assertIn("No eligible operations observed", rendered)

    def test_no_samples_render_as_unknown_not_zero_percent(self) -> None:
        payload = {
            **self.payload,
            "workers": {
                **self.payload["workers"],
                "total": 0,
                "succeeded": 0,
                "citations": 0,
            },
            "derived": {
                **self.payload["derived"],
                "worker_success_rate": 0.0,
                "citation_validity_rate": 0.0,
                "worker_stage_credit_difference": 0.0,
                "worker_stage_credit_savings_rate": 0.0,
            },
            "by_model": [],
        }
        rendered = _render_stats(payload, "7d", color=False)
        self.assertIn("CREDITS SAVED      —", rendered)
        self.assertIn("SUCCESS RATE       —", rendered)
        self.assertIn("CITATION VALIDITY  —", rendered)
        self.assertNotIn("SUCCESS RATE       0.0%", rendered)

    def test_shadow_context_is_identified_but_not_counted_as_avoided(self) -> None:
        payload = {
            **self.payload,
            "routing": {
                "total": 2,
                "routed": 2,
                "enforced": 0,
                "estimated_tokens": 15400,
                "estimated_intercepted_tokens": 0,
            },
        }
        rendered = _render_stats(payload, "all", color=False)
        self.assertIn("CONTEXT AVOIDED    0 tokens", rendered)
        self.assertIn("15.40K more identified", rendered)
        self.assertIn("0 enforced · 2 observed with strict routing off", rendered)

    def test_color_mode_and_parser_override(self) -> None:
        rendered = _render_stats(self.payload, "all", color=True)
        self.assertIn("\033[", rendered)
        with patch.dict(os.environ, {"NO_COLOR": ""}, clear=False):
            self.assertFalse(_color_enabled("auto"))
            self.assertTrue(_color_enabled("always"))
        args = build_parser().parse_args(["stats", "--color", "never"])
        self.assertEqual(args.color, "never")


if __name__ == "__main__":
    unittest.main()
