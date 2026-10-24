"""Portable SDK-record replay, NOT native Windows origin/ownership proof."""
import copy
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def module():
    spec = importlib.util.spec_from_file_location('task_origin', HERE / 'task_origin.py')
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def replay() -> tuple[dict, dict, dict, dict, list[dict]]:
    # These are intentionally invented records for the contract checker, not
    # alleged Windows event IDs, field mappings, or a captured native success.
    sid = 'S-1-5-21-1-2-3-1001'
    identities = [(400, 100), (401, 110), (402, 120)]
    expected = dict(task='\\CloverGateway', sid=sid, instance='{instance}',
                    request_start=90, request_end=125, source_sha='a' * 40,
                    source_tree='b' * 40, home='C:\\Users\\fresh\\clover',
                    action_image='C:\\Windows\\System32\\wscript.exe',
                    action_argv=['wscript.exe', '//B', '//Nologo', 'C:\\private\\gateway.vbs'],
                    root_image='C:\\private\\venv\\Scripts\\python.exe',
                    worker_image='C:\\managed\\python.exe',
                    root_argv=['C:\\private\\venv\\Scripts\\python.exe', '-m', 'clover_cli.main', 'gateway', 'run'],
                    worker_argv=['C:\\private\\venv\\Scripts\\python.exe', '-m', 'clover_cli.main', 'gateway', 'run'])
    snapshot = dict(task=expected['task'], principal_sid=sid,
                    logon='InteractiveToken', run_level='LeastPrivilege',
                    action_image=expected['action_image'], action_argv=expected['action_argv'],
                    source_sha=expected['source_sha'], source_tree=expected['source_tree'],
                    home=expected['home'], xml_sha256='c' * 64, launcher_sha256='d' * 64,
                    registration_identity='registration-1')
    events = [dict(kind='registration', at=95, task=expected['task'],
                   registration_identity='registration-1')]
    events += [dict(kind='process', at=start, pid=pid, start=start, sid=sid,
                    image=expected[role + '_image'], argv=expected[role + '_argv'],
                    parent=list(identities[n - 1]) if n else [300, 50])
               for n, ((pid, start), role) in enumerate(zip(identities, ['action', 'root', 'worker']))]
    events += [dict(kind='task-action', at=101, task=expected['task'], instance=expected['instance'],
                    registration_identity='registration-1', action=list(identities[0]), sid=sid)]
    events.sort(key=lambda e: e['at'])
    for n, event in enumerate(events):
        event['sequence'] = n + 1
    live = [dict(e) for e in events if e['kind'] == 'process' and e['pid'] != 400]
    capture = dict(evidence_kind='portable-sdk-record-replay', schema='modeled-contract-v1',
                   armed_at=80, sealed_at=130, task_preexisted=False, gateway_preexisted=False,
                   dropped=0, callback_errors=[], events=events,
                   schedule_before=dict(pid=300, start=50, status='running'),
                   schedule_after=dict(pid=300, start=50, status='running'))
    return capture, expected, snapshot, copy.deepcopy(snapshot), live


def test_exited_normal_vbs_accepts_only_complete_historical_replay_not_stop_authority():
    m = module()
    result = m.verify_replay(*replay())
    assert result['status'] == 'PORTABLE_REPLAY_ONLY'
    assert result['action'] == [400, 100]
    assert result['root'] == [401, 110]
    assert result['worker'] == [402, 120]
    assert result['stop_authority'] is False
    assert result['native_acceptance'] is False


@pytest.mark.parametrize('control', [
    'unknown-schema', 'missing-event', 'dropped', 'callback-error', 'old-event',
    'reused-pid', 'stale-parent', 'foreign-sid', 'manual-matching-launch',
    'wrong-instance', 'ambiguous-instance', 'preexisting-task', 'preexisting-gateway',
    'replaced-task', 'same-bytes-new-registration', 'launcher-change', 'source-change',
    'foreign-principal', 's4u-principal', 'wrong-action', 'wrong-root-image',
    'live-worker-reuse', 'live-root-argv', 'schedule-pid-reuse', 'schedule-stopped',
    'dropped-sequence', 'late-collector', 'missing-live-root', 'engine-pid-is-not-action',
])
def test_adversarial_portable_replays_refuse(control):
    m = module()
    c, e, initial, reread, live = replay()
    processes = [x for x in c['events'] if x['kind'] == 'process']
    action = next(x for x in c['events'] if x['kind'] == 'task-action')
    if control == 'unknown-schema': c['schema'] = 'unprobed-native-schema'
    elif control == 'missing-event': c['events'].remove(processes[0])
    elif control == 'dropped': c['dropped'] = 1
    elif control == 'callback-error': c['callback_errors'] = ['dropped native callback']
    elif control == 'old-event': processes[0]['at'] = 79
    elif control == 'reused-pid': processes[1]['pid'] = processes[0]['pid']
    elif control == 'stale-parent': processes[1]['parent'][1] -= 1
    elif control == 'foreign-sid': processes[0]['sid'] = 'S-1-5-18'
    elif control == 'manual-matching-launch': action['action'] = [800, 100]
    elif control == 'wrong-instance': action['instance'] = '{another-instance}'
    elif control == 'ambiguous-instance': c['events'].append(dict(action))
    elif control == 'preexisting-task': c['task_preexisted'] = True
    elif control == 'preexisting-gateway': c['gateway_preexisted'] = True
    elif control == 'replaced-task': c['events'].append(dict(kind='task-updated', at=128, sequence=6))
    elif control == 'same-bytes-new-registration': reread['registration_identity'] = 'registration-2'
    elif control == 'launcher-change': reread['launcher_sha256'] = 'e' * 64
    elif control == 'source-change': reread['source_sha'] = 'e' * 40
    elif control == 'foreign-principal': initial['principal_sid'] = reread['principal_sid'] = 'S-1-5-18'
    elif control == 's4u-principal': initial['logon'] = reread['logon'] = 'S4U'
    elif control == 'wrong-action': processes[0]['argv'] = ['wscript.exe', 'foreign.vbs']
    elif control == 'wrong-root-image': processes[1]['image'] = 'foreign-venv-python.exe'
    elif control == 'live-worker-reuse': live[1]['start'] += 1
    elif control == 'live-root-argv': live[0]['argv'] = ['python.exe', 'manual.py']
    elif control == 'schedule-pid-reuse': c['schedule_after']['start'] += 1
    elif control == 'schedule-stopped': c['schedule_after']['status'] = 'stopped'
    elif control == 'dropped-sequence': c['events'][0]['sequence'] = 0
    elif control == 'late-collector': c['armed_at'] = 96
    elif control == 'missing-live-root': live.pop(0)
    elif control == 'engine-pid-is-not-action': action['action'] = [300, 50]
    with pytest.raises(m.OriginRefused):
        m.verify_replay(c, e, initial, reread, live)


def test_sdk_schema_is_read_from_xml_not_inferred_from_event_id():
    m = module()
    # Small literal XML input, not a recorded native callback or admitted schema.
    raw = '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><Provider Name="fixture" Guid="{00000000-0000-0000-0000-000000000001}"/><EventID>129</EventID><Version>0</Version></System><EventData><Data Name="EnginePID">300</Data><Data Name="TaskName">\\CloverGateway</Data></EventData></Event>'
    schema = m.sdk_schema(raw)
    assert schema['fields'] == ['EnginePID', 'TaskName']
    assert schema['event_id'] == 129
    assert 'action_pid' not in schema
    assert '300' not in str(schema), 'schema inventory must not expose payload values'
    with pytest.raises(m.OriginRefused):
        m.sdk_schema(raw.replace('Name="TaskName"', 'Name="EnginePID"'))


def test_replay_label_can_never_enable_native_acceptance(tmp_path):
    m = module()
    (tmp_path / 'native.json').write_text(__import__('json').dumps(replay()[0]))
    with pytest.raises(m.OriginRefused, match='no SDK schema'):
        m.refuse_unvalidated_native(tmp_path)
    blocker = __import__('json').loads((tmp_path / 'task-origin-blocker.json').read_text())
    assert blocker['supported_native_adapters'] == []
    assert blocker['native_acceptance'] is False


def test_live_schedule_d10_contract_stays_separate():
    m = module()
    observation = dict(supervisor=dict(schedule_status='running', schedule_pid=300,
                                       ancestors=[dict(pid=300)]))
    assert m.live_schedule_ancestor(observation)['public_task_acceptance'] is False
    observation['supervisor']['ancestors'] = []
    with pytest.raises(m.OriginRefused, match='LIVE Schedule'):
        m.live_schedule_ancestor(observation)


@pytest.mark.parametrize('delta', [{}, {'schedule_pid': 301}, {'schedule_create_time': 1.26}, {'schedule_status': 'stopped'}])
def test_native_observation_schedule_uses_creation_identity_not_pid_alone(delta):
    m = module()
    before = dict(schedule_pid=300, schedule_create_time=1.25, schedule_status='running')
    after = dict(before, **delta)
    if delta:
        with pytest.raises(m.OriginRefused): m.schedule_observation_unchanged(before, after)
    else:
        m.schedule_observation_unchanged(before, after)



def test_public_probe_arms_real_sdk_before_supported_registration_and_owned_task_start():
    text = (HERE / 'start_troy.ps1').read_text()
    assert text.index('$collector.Subscribe()') < text.index('& $Python @argv') < text.index('$instance = $task.Run($null)')
    assert "'--no-start-now', '--start-on-login'" in text
    assert 'New-ScheduledTaskAction' not in text
    assert 'New-ScheduledTaskPrincipal' not in text
    assert "if (-not $admin) { throw" in text
    assert "if (-not $ProbeOnly) { throw" in text
    assert text.index('$reread = Read-InstalledTask') < text.index("$phase = 'native-schema-admission'")
    embedded = text.split("$renderCheck = @'\n", 1)[1].split("\n'@", 1)[0]
    compile(embedded, 'installed-renderer-observation', 'exec')
    assert '._write_task_script(' not in embedded


def test_original_direct_task_source_is_retained_as_labeled_d10_diagnostic():
    import subprocess
    original = subprocess.check_output(['git', 'show', 'b1b7eb5e0fb42a81462ee70cead5467964dd0703:.github/owner-worker-proof/start_troy.ps1'], cwd=HERE, text=True)
    retained = (HERE / 'start_troy_live_ancestor_diagnostic.ps1').read_text()
    assert retained.split('\n', 1)[1] == original
    assert 'NOT PUBLIC TASK ACCEPTANCE' in retained.split('\n')[0]


def test_actual_full_driver_refuses_task_before_install_or_bootstrap_resolution(tmp_path):
    import ast
    from types import SimpleNamespace
    m = module()
    tree = ast.parse((HERE / 'windows_update.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    isolated = ast.Module(body=[fn], type_ignores=[])
    namespace: dict = dict(native_guard=lambda: None, Path=Path, task_origin=m,
                     __file__=str(HERE / 'windows_update.py'))
    exec(compile(isolated, 'actual-driver-task-preflight', 'exec'), namespace)
    with pytest.raises(m.OriginRefused, match='no SDK schema'):
        namespace['main'](SimpleNamespace(setup='troy', evidence=str(tmp_path)))
    assert (tmp_path / 'task-origin-blocker.json').is_file()



def test_original_update_delivery_foreign_scm_and_stability_assertions_are_retained():
    import ast
    import subprocess
    original = subprocess.check_output(['git', 'show', 'b1b7eb5e0fb42a81462ee70cead5467964dd0703:.github/owner-worker-proof/windows_update.py'], cwd=HERE, text=True)
    current = (HERE / 'windows_update.py').read_text()

    def tail(text):
        main = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        launch = next(n for n in main.body if isinstance(n, ast.Try) and any(isinstance(a, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'before' for t in a.targets) for a in n.body))
        start = next(i for i, n in enumerate(launch.body) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'before' for t in n.targets))
        return ast.Module(body=launch.body[start:] + launch.finalbody, type_ignores=[])

    class OnlyOriginAndStrengthenedSchedule(ast.NodeTransformer):
        def visit_If(self, node):
            if isinstance(node.test, ast.Name) and node.test.id == 'diagnostic' and len(node.body) == 1 and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Call) and isinstance(node.body[0].value.func, ast.Attribute) and node.body[0].value.func.attr == 'live_schedule_ancestor':
                return None  # The explicitly separate D10 origin assertion.
            return self.generic_visit(node)

        def visit_Call(self, node):
            for keyword in node.keywords:
                if keyword.arg == 'status' and isinstance(keyword.value, ast.IfExp) and isinstance(keyword.value.test, ast.Name) and keyword.value.test.id == 'diagnostic':
                    assert ast.literal_eval(keyword.value.body) == 'SYNTHETIC_D10_DIAGNOSTIC_PASS'
                    assert ast.literal_eval(keyword.value.orelse) == 'PASS'
                    keyword.value = ast.Constant(value='PASS')
                    node.keywords = [k for k in node.keywords if k.arg != 'diagnostic_only']
            return self.generic_visit(node)

        def visit_Assert(self, node):
            dump = ast.dump(node.test)
            if "id='via_schedule'" in dump or "value='schedule_pid'" in dump:
                return None
            return self.generic_visit(node)

        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == 'schedule_observation_unchanged':
                return None
            return self.generic_visit(node)

    old_tail, new_tail = tail(original), tail(current)
    schedule_calls = [n for n in ast.walk(new_tail) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'schedule_observation_unchanged']
    assert len(schedule_calls) == 4, 'after-update, every stable sample, final and refusal samples'
    assert ast.dump(OnlyOriginAndStrengthenedSchedule().visit(old_tail)) == ast.dump(OnlyOriginAndStrengthenedSchedule().visit(new_tail)), 'Unapproved delivery/ownership/foreign-SCM/stability delta'



def test_separate_d10_driver_routes_original_task_and_enforces_live_ancestor(tmp_path):
    import ast
    from types import SimpleNamespace
    m = module()
    main = next(n for n in ast.parse((HERE / 'windows_update.py').read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    launch = next(n for n in main.body if isinstance(n, ast.Try) and any(isinstance(a, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'before' for t in a.targets) for a in n.body))
    calls = []
    namespace: dict = dict(WINDOWS=True, diagnostic=True, run=lambda *a, **kw: calls.append(a), Path=Path,
                     script=str(HERE / 'windows_update.py'), python=lambda _: Path('installed/python.exe'),
                     live=Path('installed'), source=Path('source'), evidence=tmp_path)
    exec(compile(ast.Module(body=[launch.body[0]], type_ignores=[]), 'actual-d10-launch-branch', 'exec'), namespace)
    assert len(calls) == 1
    assert HERE / 'start_troy_live_ancestor_diagnostic.ps1' in calls[0][0]
    assert calls[0][0][calls[0][0].index('-Python') + 1] == Path('installed/pythonw.exe')
    assert namespace['service']['diagnostic_only'] is True
    origin = next(n for n in launch.body if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == 'diagnostic')
    observation = dict(supervisor=dict(schedule_status='running', schedule_pid=300, ancestors=[dict(pid=300)]))
    namespace.update(task_origin=m, before=observation)
    exec(compile(ast.Module(body=[origin], type_ignores=[]), 'actual-d10-origin-branch', 'exec'), namespace)
    observation['supervisor']['ancestors'] = []
    with pytest.raises(m.OriginRefused, match='LIVE Schedule'):
        exec(compile(ast.Module(body=[origin], type_ignores=[]), 'actual-d10-origin-branch', 'exec'), namespace)
    status = next(k.value for n in ast.walk(main) if isinstance(n, ast.Call) for k in n.keywords if k.arg == 'status' and isinstance(k.value, ast.IfExp))
    assert eval(compile(ast.Expression(body=status), 'actual-d10-result-label', 'eval'), dict(diagnostic=True)) == 'SYNTHETIC_D10_DIAGNOSTIC_PASS'
