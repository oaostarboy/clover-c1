"""Deterministic proactive controls; never the legacy recurring-loop alias."""
from clover_cli.commands import resolve_command, GATEWAY_KNOWN_COMMANDS


def test_proactive_is_separate_discoverable_command():
    command = resolve_command("proactive")
    assert command is not None
    assert command.name == "proactive"
    loop = resolve_command("loop")
    assert loop is not None
    assert "proactive" not in loop.aliases
    assert "proactive" in GATEWAY_KNOWN_COMMANDS
    assert {"status", "on", "off"} <= set(command.subcommands)
    assert command.busy_policy == "dispatch"


def test_help_autocomplete_and_telegram_menu_derive_real_command():
    from clover_cli.commands import COMMANDS, SUBCOMMANDS, gateway_help_lines, telegram_bot_commands
    assert "/proactive" in COMMANDS
    assert SUBCOMMANDS["/proactive"] == ["status", "on", "off"]
    assert any("/proactive" in line for line in gateway_help_lines())
    assert "proactive" in dict(telegram_bot_commands(include_plugins=False))


def test_slack_catchall_keeps_proactive_accessible_without_displacing_usage():
    from clover_cli.commands import slack_subcommand_map, slack_native_slashes
    assert slack_subcommand_map()["proactive"] == "/proactive"
    assert "usage" in {name for name, _, _ in slack_native_slashes()}
