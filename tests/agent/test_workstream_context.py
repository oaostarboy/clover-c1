"""Workstream context must share Clover's current-turn API-content composer."""

from agent.turn_context import compose_user_api_content


def test_workstream_context_is_appended_only_to_the_current_turn_api_copy():
    clean = "What did we decide about rollout?"
    note = "[ws:w-1@r2] Keep rollout private."

    composed = compose_user_api_content(
        clean,
        ext_prefetch_cache="",
        plugin_user_context="",
        work_plan="",
        workstream_context=note,
    )

    assert composed == clean + "\n\n" + note
    assert clean == "What did we decide about rollout?"


def test_empty_workstream_context_preserves_existing_byte_identity():
    content = "Same bytes, please."

    assert compose_user_api_content(
        content,
        ext_prefetch_cache="",
        plugin_user_context="",
        work_plan="",
        workstream_context="",
    ) is None
