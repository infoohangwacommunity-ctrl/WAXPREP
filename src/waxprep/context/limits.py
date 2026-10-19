"""Safety limits for Context Intelligence investigations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextSafetyLimits:
    max_investigation_steps: int = 8
    max_tool_calls: int = 16
    max_results_per_tool: int = 8
    max_semantic_candidates: int = 5000
    max_evidence_items: int = 12
    max_single_evidence_chars: int = 3000
    max_total_context_chars: int = 12000
    max_model_output_tokens: int = 1200
