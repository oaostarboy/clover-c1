"""A fresh explicit return may revalidate a weak exact-owner snapshot.

An old after-reply lease must not become a forever-secret context. The new
opportunity uses no new account, main selection, key or inferred identity link.
"""
from types import SimpleNamespace
import pytest
from agent import presence_authority as handoff
from tests.test_presence_selected_owner_desktop import test_desktop_selected_owner_not_identity_link_owns_callback as desktop_case
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tui_gateway import server


@pytest.mark.parametrize('source', ['exact', 'no-source'])
def test_fresh_project_return_revalidates_exact_main_owner_after_old_callback_lease(monkeypatch,tmp_path,source):
    tick=[0]
    monkeypatch.setattr(handoff,'time',SimpleNamespace(monotonic=lambda:tick[0]))
    dispatch=server.dispatch
    def later(request,*args,**kwargs):
        if request.get('method')=='presence.project_return':tick[0]=31
        return dispatch(request,*args,**kwargs)
    monkeypatch.setattr(server,'dispatch',later)
    desktop_case(monkeypatch,tmp_path,'project_return',source)
