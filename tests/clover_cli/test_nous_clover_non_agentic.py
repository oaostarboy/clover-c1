"""Tests for the Clover-Clover-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"clover"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``clover-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "clover" tag namespace.

``is_nous_clover_non_agentic`` should only match the actual Clover Cognition
Clover-3 / Clover-4 chat family.
"""

from __future__ import annotations

import pytest

from clover_cli.model_switch import (
    _CLOVER_MODEL_WARNING,
    _check_clover_model_warning,
    is_nous_clover_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "Clover Cognition/Clover-3-Llama-3.1-70B",
        "Clover Cognition/Clover-3-Llama-3.1-405B",
        "clover-3",
        "Clover-3",
        "clover-4",
        "clover-4-405b",
        "clover_4_70b",
        "openrouter/clover3:70b",
        "openrouter/clovercognition/clover-4-405b",
        "Clover Cognition/Clover3",
        "clover-3.1",
    ],
)
def test_matches_real_nous_clover_chat_models(model_name: str) -> None:
    assert is_nous_clover_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Clover Clover 3/4"
    )
    assert _check_clover_model_warning(model_name) == _CLOVER_MODEL_WARNING


