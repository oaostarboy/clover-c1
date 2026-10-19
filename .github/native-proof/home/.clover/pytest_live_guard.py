"""Offline regression guard: synthetic loopback HTTP only, no external network.

Load with pytest -p qa.presence.network_guard. Existing wire-invariant tests use
real local HTTP servers; native SDK MockTransport tests need no sockets.
"""
import ipaddress
import socket
import pytest


def _local(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def presence_offline_network_guard(monkeypatch):
    resolve, create = socket.getaddrinfo, socket.create_connection
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def check(address):
        if not isinstance(address, tuple) or not _local(address[0]):
            raise AssertionError('external networking is forbidden in presence integration regression')
    def local_resolve(host, *args, **kwargs):
        if not _local(host):
            raise AssertionError('external DNS is forbidden in presence integration regression')
        return resolve(host, *args, **kwargs)
    def local_create(address, *args, **kwargs):
        check(address)
        return create(address, *args, **kwargs)
    def local_connect(sock, address):
        if sock.family != getattr(socket, 'AF_UNIX', None):
            check(address)
        return connect(sock, address)
    def local_connect_ex(sock, address):
        if sock.family != getattr(socket, 'AF_UNIX', None):
            check(address)
        return connect_ex(sock, address)
    monkeypatch.setattr(socket, 'getaddrinfo', local_resolve)
    monkeypatch.setattr(socket, 'create_connection', local_create)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', local_connect_ex)


import hashlib, json, os
from pathlib import Path
import pytest
SOURCE=Path(__file__).resolve().parents[5]/'source'
DEST=Path(__file__).resolve().parents[5]/'evidence'
TRACKED=set(json.loads((Path(__file__).resolve().parents[2]/'presence-manifest.json').read_text())['tracked_test_paths'])
REC=None
REPORTS=[]
@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global REC
    files=[]
    for arg in config.args:
        try:
            p=Path(str(arg)).resolve()
            rel=p.relative_to(SOURCE.resolve()).as_posix()
            if rel in TRACKED and p.is_file(): files.append(rel)
        except (ValueError,OSError): pass
    if len(files)!=1: return
    key=hashlib.sha256(files[0].encode()).hexdigest()[:20]+'-'+str(os.getpid())
    REC={'file':files[0],'key':key,'parent_pid':os.getppid(),'args':list(map(str,config.invocation_params.args)),'reports':REPORTS}
    config.option.xmlpath=str(DEST/(key+'.xml'))
def pytest_runtest_logreport(report):
    _note('phase',nodeid=report.nodeid,when=report.when,outcome=report.outcome)
    if REC is not None:
        REPORTS.append({'nodeid':report.nodeid,'when':report.when,'outcome':report.outcome,'wasxfail':getattr(report,'wasxfail',None),'longrepr':str(report.longrepr) if report.failed or report.skipped else None})
def pytest_collectreport(report):
    if REC is not None and report.failed:
        REPORTS.append({'nodeid':report.nodeid,'when':'collection','outcome':'failed','longrepr':str(report.longrepr)})
def pytest_sessionfinish(session,exitstatus):
    if REC is not None:
        REC.update(exitstatus=int(exitstatus),collected=session.testscollected)
def pytest_unconfigure(config):
    if REC is not None: (DEST/(REC['key']+'.json')).write_text(json.dumps(REC),encoding='utf-8')

# Diagnostic-only observer; never changes an application result or deadline.
import faulthandler, threading, time, traceback, sys
_DIAG = None
_STACK = None
_T0 = time.monotonic()
def _note(kind, **values):
    if _DIAG is not None:
        _DIAG.write(json.dumps(dict(t=round(time.monotonic()-_T0,6),kind=kind,**values))+"\n")
        _DIAG.flush()
_original_thread_start = threading.Thread.start
def _observed_start(thread, *args, **kwargs):
    target = getattr(thread, '_target', None)
    if getattr(target, '__module__', '') == 'clover_cli.banner':
        _note('banner_thread_start', frames=traceback.format_stack(limit=12))
    return _original_thread_start(thread, *args, **kwargs)
threading.Thread.start = _observed_start
@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    global _DIAG, _STACK
    _DIAG=(DEST/('diagnostic-'+str(os.getpid())+'.jsonl')).open('w', encoding='utf-8')
    _STACK=(DEST/('stacks-'+str(os.getpid())+'.log')).open('w', encoding='utf-8')
    _note('sessionstart')
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    _note('test_start',nodeid=nodeid)
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # Install only after the test has naturally loaded/registers its callback.
    # No global Python profiling, eager SDK imports, extra reads or new budgets.
    import functools
    module=sys.modules.get('gateway.platforms.base')
    patches=[]
    installed=False
    if module is not None:
        cls=module.BasePlatformAdapter
        register=cls.register_post_delivery_callback
        @functools.wraps(register)
        def observed_register(self,*args,**kwargs):
            nonlocal installed
            if not installed and 'agent.presence_runtime' in sys.modules:
                installed=True
                import agent.presence_authority as authority
                from cron.presence import ThoughtStore
                validate=authority.validate_active
                unknown=ThoughtStore.unknown
                def remaining():
                    active=authority._ACTIVE.get()
                    return active[2]-time.monotonic() if active is not None and active[2] is not None else None
                @functools.wraps(validate)
                def observed_validate():
                    try:
                        return validate()
                    except BaseException as exc:
                        _note('authority_fence_refusal',exception=type(exc).__name__,remaining=remaining())
                        raise
                @functools.wraps(unknown)
                def observed_unknown(self,*a,**kw):
                    before=remaining()
                    result=unknown(self,*a,**kw)
                    _note('unknown_finalizer',returned=type(result).__name__,remaining_before=before,remaining_after=remaining())
                    return result
                patches.extend([(authority,'validate_active',validate),(ThoughtStore,'unknown',unknown)])
                authority.validate_active=observed_validate
                ThoughtStore.unknown=observed_unknown
            return register(self,*args,**kwargs)
        patches.append((cls,'register_post_delivery_callback',register))
        cls.register_post_delivery_callback=observed_register
    try:
        yield
    finally:
        for obj,name,value in reversed(patches):setattr(obj,name,value)
