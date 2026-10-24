"""Fixture-only Task-origin contracts. Never imported by the product.

The portable format below models the *required* SDK witness, not a claimed
Windows schema. No native schema adapter is admitted yet. A native callback
probe must establish the actual Task action/instance and process start-identity
fields before one can be reviewed. EnginePID is deliberately not an action PID.
Historical origin is never permission to stop any process or service.
"""
from __future__ import annotations

import json
from pathlib import Path


class OriginRefused(ValueError):
    pass


def require(value, reason):
    if not value:
        raise OriginRefused(reason)


def process_identity(row):
    pid, start = row['pid'], row['start']
    require(type(pid) is int and pid > 0 and type(start) is int and start > 0,
            'PID and exact native creation/start identity required')
    return [pid, start]


def schedule_unchanged(before, after):
    require(before['status'] == after['status'] == 'running', 'Schedule not running')
    require(process_identity(before) == process_identity(after), 'Schedule identity changed')


def schedule_observation_unchanged(before, after):
    """Native observer samples: PID equality alone cannot rule out PID reuse."""
    require(before['schedule_status'] == after['schedule_status'] == 'running',
            'Schedule not running')
    require(before['schedule_pid'] == after['schedule_pid'] and
            before['schedule_create_time'] == after['schedule_create_time'],
            'Schedule PID/creation identity changed')


def live_schedule_ancestor(observation):
    """Original D10 contract, ONLY for the direct-python/S4U diagnostic."""
    supervisor = observation['supervisor']
    require(supervisor['schedule_status'] == 'running', 'Schedule not running')
    require(supervisor['schedule_pid'] in [p['pid'] for p in supervisor['ancestors']],
            'D10 diagnostic requires original LIVE Schedule ancestor')
    return dict(evidence_kind='SYNTHETIC_DIRECT_TASK_D10_DIAGNOSTIC',
                public_task_acceptance=False, live_schedule_ancestor=True)


def verify_replay(capture, expected, initial, reread, live):
    """Strict PORTABLE contract replay; cannot produce a native PASS receipt."""
    require(capture['evidence_kind'] == 'portable-sdk-record-replay' and
            capture['schema'] == 'modeled-contract-v1', 'not a portable modeled SDK record replay')
    require(capture['dropped'] == 0 and not capture['callback_errors'], 'missing/dropped callback')
    require(capture['task_preexisted'] is False and capture['gateway_preexisted'] is False,
            'task or gateway preexisted')
    require(capture['armed_at'] < expected['request_start'] <= expected['request_end'] < capture['sealed_at'],
            'collector must surround registration/start and pre-update recheck')
    schedule_unchanged(capture['schedule_before'], capture['schedule_after'])
    require(initial == reread, 'task/source/launcher changed before update')
    for key in ('task', 'source_sha', 'source_tree', 'home', 'action_image', 'action_argv'):
        require(initial[key] == expected[key], 'installed snapshot mismatch: ' + key)
    require(initial['principal_sid'] == expected['sid'] and
            initial['logon'] == 'InteractiveToken' and initial['run_level'] == 'LeastPrivilege',
            'not the public principal')
    for key in ('xml_sha256', 'launcher_sha256'):
        require(len(initial[key]) == 64 and set(initial[key]) <= set('0123456789abcdef'),
                'missing exact installed-byte hash')
    events = capture['events']
    require([e['sequence'] for e in events] == list(range(1, len(events) + 1)),
            'missing/duplicate/out-of-order callback sequence')
    require([e['at'] for e in events] == sorted(e['at'] for e in events), 'unordered event clock')
    require(all(capture['armed_at'] <= e['at'] <= capture['sealed_at'] for e in events), 'old event')
    require(all(e['kind'] in ('registration', 'task-action', 'process') for e in events),
            'unknown event schema or task mutation')
    registrations = [e for e in events if e['kind'] == 'registration']
    require(len(registrations) == 1, 'missing/ambiguous/replaced task registration')
    registration = registrations[0]
    require(registration['task'] == expected['task'] and
            registration['registration_identity'] == initial['registration_identity'] and
            expected['request_start'] <= registration['at'] <= expected['request_end'],
            'wrong/stale task registration')
    actions = [e for e in events if e['kind'] == 'task-action']
    require(len(actions) == 1, 'missing/ambiguous exact task instance action')
    action = actions[0]
    require(action['task'] == expected['task'] and action['instance'] == expected['instance'] and
            action['registration_identity'] == initial['registration_identity'] and
            action['sid'] == expected['sid'] and registration['at'] <= action['at'] <= expected['request_end'],
            'wrong/stale task action/instance/SID')
    processes = [e for e in events if e['kind'] == 'process']
    require(len(processes) == 3, 'missing/ambiguous/reused process history')
    require(len({e['pid'] for e in processes}) == 3, 'PID reuse or duplicate process callback')
    chain = []
    parent = action['action']
    for role in ('action', 'root', 'worker'):
        matches = [e for e in processes if
                   (process_identity(e) == parent if role == 'action' else e['parent'] == parent)]
        require(len(matches) == 1, 'missing/ambiguous historical identity edge: ' + role)
        row = matches[0]
        require(row['sid'] == expected['sid'] and row['image'] == expected[role + '_image'] and
                row['argv'] == expected[role + '_argv'], 'foreign image/argv/SID: ' + role)
        require(registration['at'] <= row['start'] == row['at'] <= expected['request_end'],
                'stale or non-creation process event')
        if chain:
            require(chain[-1]['start'] < row['start'], 'reused/unordered parent identity')
        chain.append(row)
        parent = process_identity(row)
    require(chain[0]['start'] <= action['at'], 'action event predates action creation')
    require(len(live) == 2, 'independent exact live root/worker required')
    for historical, current in zip(chain[1:], live):
        for key in ('pid', 'start', 'sid', 'image', 'argv', 'parent'):
            require(historical[key] == current[key], 'live identity changed: ' + key)
    return dict(status='PORTABLE_REPLAY_ONLY', action=process_identity(chain[0]),
                root=process_identity(chain[1]), worker=process_identity(chain[2]),
                native_acceptance=False, stop_authority=False)


def sdk_schema(raw):
    """Inventory SDK XML shape only. Event numbers never imply PID semantics."""
    import hashlib
    import xml.etree.ElementTree as ET
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    try:
        root = ET.fromstring(raw)
        system = root.find('e:System', ns)
        if system is None:
            raise OriginRefused('missing SDK System metadata')
        provider = system.find('e:Provider', ns)
        if provider is None:
            raise OriginRefused('missing SDK Provider metadata')
        fields = [d.attrib['Name'] for d in root.findall('e:EventData/e:Data', ns)]
        require(len(fields) == len(set(fields)), 'duplicate/ambiguous SDK field names')
        return dict(provider=provider.attrib['Name'], guid=provider.attrib['Guid'],
                    event_id=int(system.findtext('e:EventID', namespaces=ns) or ''),
                    version=int(system.findtext('e:Version', namespaces=ns) or ''), fields=fields,
                    xml_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest())
    except (ET.ParseError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise OriginRefused('unsupported SDK event XML schema') from exc


def session_diagnostic(environment):
    """Classify measured native facts only; never authorize a token or a Task.

    WTSActive (0), an actual matching LSA interactive/remote-interactive logon,
    matching session/SID, and an actual medium token are all necessary. Merely
    possessing INTERACTIVE in TokenGroups or specifying LOGON_INTERACTIVE is not
    proof. Reusing the runner's linked token requires separate parent review.
    """
    def medium_interactive(token):
        if not isinstance(token, dict) or token.get('errors') != []:
            return False
        sid, session = token.get('sid'), token.get('session_id')
        return (isinstance(sid, str) and sid.startswith('S-1-5-21-') and
                type(session) is int and session > 0 and
                token.get('elevated') is False and
                token.get('elevation_type') in (1, 3) and
                token.get('integrity_sid') == 'S-1-16-8192' and
                token.get('interactive_group') is True and
                token.get('logon_type') in (2, 10) and
                token.get('logon_sid') == sid and token.get('logon_session_id') == session and
                len([s for s in environment.get('sessions', [])
                     if s.get('session_id') == session and s.get('state') == 0 and
                     s.get('sid') == sid and s.get('error') is None]) == 1)

    candidate = 'NONE'
    current, linked = environment.get('current'), environment.get('linked')
    if environment.get('errors') == []:
        if medium_interactive(current):
            candidate = 'CURRENT_MEDIUM_INTERACTIVE'
        elif (isinstance(current, dict) and current.get('errors') == [] and
              current.get('elevated') is True and current.get('elevation_type') == 2 and
              medium_interactive(linked) and linked['sid'] == current.get('sid') and
              linked['session_id'] == current.get('session_id')):
            candidate = 'EXISTING_LINKED_MEDIUM_REQUIRES_REVIEW'
    return dict(status='DIAGNOSTIC_ONLY', session_candidate=candidate,
                review_required=True, native_acceptance=False,
                public_registration_attempted=False, stop_authority=False)


def refuse_unvalidated_native(evidence):
    """Fail closed. A caller-supplied JSON label can NEVER enable native proof."""
    path = Path(evidence)
    path.mkdir(parents=True, exist_ok=True)
    blocker = dict(status='NEEDS_NATIVE_SCHEMA_PROBE', native_acceptance=False,
        stop_authority=False, supported_native_adapters=[],
        needed=['genuine fresh-account interactive logon for public InteractiveToken',
                'public registration without silently elevating the non-admin account',
                'SDK Task-instance -> actual action PID/start/SID schema (not EnginePID)',
                'SDK process creation image/argv/SID and creation/start identity through exited VBS',
                'reviewed native adapter plus positive and adversarial native controls'],
        probe='start_troy.ps1 -Python <installed> -Source <installed> -Evidence <private> -ExpectedSid <created-account-SID> -ProbeOnly')
    (path / 'task-origin-blocker.json').write_text(json.dumps(blocker, indent=2) + '\n', encoding='utf-8')
    raise OriginRefused('Public Task native origin NOT EXERCISED; see task-origin-blocker.json; no SDK schema has been admitted')
