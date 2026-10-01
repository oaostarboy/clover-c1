"""Plan-first delegation guidance is scoped to capable root agents."""

from agent.delegation_context import delegated_child_context
from agent.system_prompt import build_system_prompt_parts


def _prompt(tool_names, *, child=False, model="chosen/provider-model"):
    from types import SimpleNamespace

    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=tool_names,
        _task_completion_guidance=False,
        _parallel_tool_call_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model=model,
        provider="chosen-provider",
        platform="cli",
        pass_session_id=False,
        session_id="test-session",
        _emit_status=lambda *_args, **_kwargs: None,
    )
    if child:
        with delegated_child_context():
            stable = build_system_prompt_parts(agent)["stable"]
    else:
        stable = build_system_prompt_parts(agent)["stable"]
    assert agent.model == model
    return stable


def test_root_with_delegate_gets_plan_first_without_changing_selected_model():
    prompt = _prompt(["delegate_task"], model="chosen/provider-model")
    assert "first make a brief plan" in prompt
    assert "selected model stays the orchestrator" in prompt


def test_leaf_worker_does_not_get_root_delegation_guidance():
    prompt = _prompt(["delegate_task"], child=True)
    assert "# Plan-first delegation" not in prompt


def test_agent_without_delegation_tool_gets_no_delegation_guidance():
    prompt = _prompt(["terminal", "read_file"])
    assert "# Plan-first delegation" not in prompt


def test_guidance_preserves_quick_task_and_provider_independence_exceptions():
    prompt = _prompt(["delegate_task"])
    assert "quick answers" in prompt
    assert "never assume another provider or worker model is configured" in prompt
