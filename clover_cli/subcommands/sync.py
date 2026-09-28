"""Compatibility stub while the hosted sync command removal is deferred."""


def build_sync_parser(subparsers, *, cmd_sync):
    """Keep clover_cli.main importable until its deferred edit lands."""
    return None
