"""Bug D: the OAuth system-prompt sanitizer must not rewrite paths/slugs/code."""

from agent import anthropic_adapter as aa


def _system_text(system_prompt: str) -> list:
    kw = aa.build_anthropic_kwargs(
        model="claude-sonnet-4-5",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "hi"},
        ],
        tools=None,
        max_tokens=64,
        reasoning_config=None,
        is_oauth=True,
    )
    return [b["text"] for b in kw["system"]]


def test_identity_prefix_kept_exactly_first():
    texts = _system_text("You are a helper.")
    assert texts[0] == "You are Claude Code, Anthropic's official CLI for Claude."


def test_paths_slugs_and_code_are_never_rewritten():
    brief = (
        "Repo /home/u/Projects/clover-c1-build/clover-c1, venv "
        "/home/u/agents/clover-c1/venv, GitHub oaostarboy/clover-c1, "
        "https://github.com/oaostarboy/clover-c1/pull/1, module clover_cli, "
        "env CLOVER_HOME. Run `clover-c1 --help` and:\n"
        "```\ncd clover-c1 && echo Clover Cognition\n```\n"
        "Plain clover-c1 in a sentence stays too."
    )
    body = _system_text(brief)[1]
    for keep in (
        "/home/u/Projects/clover-c1-build/clover-c1",
        "/home/u/agents/clover-c1/venv",
        "oaostarboy/clover-c1",
        "https://github.com/oaostarboy/clover-c1/pull/1",
        "clover_cli",
        "CLOVER_HOME",
        "`clover-c1 --help`",
        "echo Clover Cognition",
        "Plain clover-c1 in a sentence",
    ):
        assert keep in body, keep
    assert "claude-code" not in body


def test_standalone_prose_brand_still_rewritten():
    body = _system_text(
        "You are Clover Cognition. The Clover agent helps. I like Clover Cognition."
    )[1]
    assert body == "You are Claude Code. The Claude Code helps. I like Claude Code."


def test_brand_inside_identifier_or_path_untouched():
    s = aa._sanitize_oauth_system_prose
    assert s("/x/Clover Cognition/y") == "/x/Clover Cognition/y"
    assert s("Clover Cognition.app") == "Clover Cognition.app"
    assert s("(Clover Cognition)") == "(Claude Code)"
