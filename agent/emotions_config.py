"""Compatibility entrypoint for the cold settings-owned status projection."""


def snapshot():
    from clover_cli.settings_projection import read_presence_status_settings

    return read_presence_status_settings()
