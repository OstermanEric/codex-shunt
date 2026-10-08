"""Standard subscription credit estimates; not observed account charges."""

from typing import Mapping

RATE_DATE = "2026-10-08"
DEFAULT_COMPARISON_MODEL = "gpt-6.1-sol"
# Credits per million input, cached input, and output tokens.
# https://learn.chatgpt.com/docs/pricing#token-rates
MODEL_RATES = {
    "gpt-6.1-sol": {"input": 50.0, "cached": 2.5, "output": 250.0},
    "gpt-6-sol": {"input": 50.0, "cached": 5.0, "output": 250.0},
    "gpt-6-luna": {"input": 2.5, "cached": 0.25, "output": 12.5},
    "gpt-6-astra": {"input": 250.0, "cached": 25.0, "output": 1250.0},
    "gpt-5.6-sol": {"input": 100.0, "cached": 10.0, "output": 500.0},
    "gpt-5.6-terra": {"input": 50.0, "cached": 5.0, "output": 300.0},
    "gpt-5.6-luna": {"input": 5.0, "cached": 0.5, "output": 30.0},
}


def credits_for(usage: Mapping[str, object], rates: Mapping[str, float]) -> float:
    incoming = max(int(usage.get("input_tokens", 0) or 0), 0)
    cached = min(max(int(usage.get("cached_input_tokens", 0) or 0), 0), incoming)
    outgoing = max(int(usage.get("output_tokens", 0) or 0), 0)
    return ((incoming - cached) * rates["input"] + cached * rates["cached"]
            + outgoing * rates["output"]) / 1_000_000
