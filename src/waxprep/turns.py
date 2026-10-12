"""Legacy turn module — re-exports from ``waxprep.orchestration``.

The Teacher-first orchestration now lives in
``waxprep.orchestration.turn``. This shim keeps imports from
``waxprep.turns`` working during the transition. New code should import
from ``waxprep.orchestration`` directly.
"""

from __future__ import annotations

from waxprep.orchestration import TurnResult, TurnService

__all__ = ["TurnResult", "TurnService"]
