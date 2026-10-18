"""Presence capabilities are resolved only on the owning authenticated transport."""
from .method_ctx import HandlerRegistry
from .presence import handle_rpc

_registry = HandlerRegistry()
method = _registry.method

for action in ('after_reply', 'ack', 'control', 'link', 'project_return'):
    def handler(rid, params, _action=action, _handle=handle_rpc):
        from tui_gateway import server
        return _handle(server, rid, params, _action)
    method('presence.'+action)(handler)


def register(server):
    _registry.install(server)
