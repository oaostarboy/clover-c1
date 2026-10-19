#!/usr/bin/env python3
"""Opt-in quality experiment. Dry-run by default; never a release accuracy claim.

Run from the candidate source with its interpreter. --execute-provider-calls may
spend the current profile's existing presence budget and send synthetic fixtures
to its configured provider. It never enables settings, changes accounts, retries,
creates thoughts, or modifies prompts/tools in a conversation. Each experiment
reserves from the same <=3/day/profile quota as application assessments.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def score_case(case, raw, *, assessment_parser=None):
    """Compare actual schema output with author proposals, NOT psychological truth.

    Attribution compares exact author annotations and preserves model owner/intent/
    source_context ONLY when the active product parser validates those fields.
    Older schemas report unavailable, not an invented model classification.
    """
    if case.get('kind') == 'connection':
        return score_connection(case, raw)
    from types import SimpleNamespace
    from agent.presence_assessment import parse as active_parser
    parse = active_parser if assessment_parser is None else assessment_parser
    from agent.presence_initiation import _unique_object
    from agent.presence_state import reading_posture

    result = {'id': case['id'], 'kind': case.get('kind', 'assessment'),
              'language': case['language'], 'status': 'measured', 'schema_valid': False,
              'prediction': None, 'evidence_literal_valid': None, 'normative_pass': None, 'error': None,
              'capture': {'status': 'not_labelled'}}
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 4096:
            raise ValueError('response bytes')
        value = json.loads(raw, object_pairs_hook=_unique_object)
        quote = value.get('thought', {}).get('quote', '') if isinstance(value, dict) and value.get('thought') else ''
        # Structural/output-safety contract separately from literal source binding.
        parse(raw, quote)
        result['schema_valid'] = True
        result['raw_prediction'] = value
        thought = value['thought']
        result['evidence_literal_valid'] = None if thought is None else thought['quote'] in case['text']
        sample = parse(raw, case['text'])
    except (ValueError, TypeError, AttributeError, UnicodeError, OverflowError, RecursionError):
        result.update(status='rejected', error='schema_or_evidence_rejected', normative_pass=False)
        return result
    result['prediction'] = sample
    affect = sample['affect']
    actual = 'abstain' if affect is None else reading_posture(SimpleNamespace(**affect))
    normative = case.get('normative', {})
    acceptable = normative.get('acceptable_postures', [case.get('expected_posture')])
    result['posture'] = {'actual': actual, 'acceptable': acceptable, 'accepted': actual in acceptable,
                         'abstained': affect is None}
    result['normative_pass'] = actual in acceptable
    if 'capture_label' not in normative:
        return result
    thought = sample['thought']
    classification = 'abstain'
    def classify(quote):
        matches = {span['classification'] for span in normative['spans'] if span['text'] == quote}
        if len(matches) == 1:
            return next(iter(matches))
        if quote == case['text'] and normative['capture_label'] != 'positive':
            return normative['attribution']
        return 'unannotated_extract'
    if thought is not None:
        classification = classify(thought['quote'])
    raw_thought = value['thought']
    fields = ('owner', 'intent', 'source_context')
    model_attribution = ({key: raw_thought[key] for key in fields}
                         if raw_thought and all(key in raw_thought for key in fields) else None)
    proposal_wrong_owner = bool(raw_thought and model_attribution
        and classify(raw_thought['quote']) in {'third_party', 'quoted'}
        and model_attribution['owner'] == 'current_human')
    correct_capture = classification == 'user_unresolved'
    positive = normative['capture_label'] == 'positive'
    outcome = ('true_positive' if correct_capture else 'wrong_capture') if thought is not None else (
        'false_negative' if positive else 'true_negative')
    accepted = ('capture_user' if correct_capture else 'abstain' if thought is None else 'wrong_capture') in normative['capture_outcomes']
    result['attribution'] = {'actual': classification, 'expected': normative['attribution'],
                             'source': 'author_annotated_exact_span_not_model_classification',
                             'model_attribution': model_attribution,
                             'model_attribution_status': 'reported_by_active_schema' if model_attribution else 'not_in_candidate_schema_or_abstained',
                             'proposal_incorrect_third_party_ownership': proposal_wrong_owner}
    result['capture'] = {'status': 'scored_author_normative', 'label': normative['capture_label'],
                         'outcome': outcome, 'accepted': accepted,
                         'incorrect_third_party_ownership': classification in {'third_party', 'quoted'},
                         'false_positive': thought is not None and not correct_capture,
                         'false_negative': positive and not correct_capture}
    result['normative_pass'] = result['normative_pass'] and accepted and not proposal_wrong_owner
    return result


def score_connection(case, raw):
    """Offline parser + lexical scaffold, NOT live publication or useful truth."""
    import math
    from types import SimpleNamespace
    from agent.presence_initiation import _parse_response, _safe_text, _unique_object
    from agent.workstreams import MIN_RELEVANCE, _tokens, relevance

    result = {'id': case['id'], 'kind': 'connection', 'language': case['language'],
              'status': 'measured', 'schema_valid': False, 'prediction': None, 'error': None,
              'normative_pass': False, 'capture': {'status': 'not_applicable'}}
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 4096:
            raise ValueError('response bytes')
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if type(value) is not dict or set(value) != {'connection', 'evidence_indices', 'confidence'}:
            raise ValueError('schema')
        text, indices, confidence = value['connection'], value['evidence_indices'], value['confidence']
        if (not isinstance(text, str) or text != '' and not _safe_text(text, 512)
                or type(confidence) not in (int, float) or not math.isfinite(confidence)
                or not 0 <= confidence <= 1 or type(indices) is not list
                or len(indices) > 2 or any(type(i) is not int or i not in (0, 1) for i in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError('schema')
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        result.update(status='rejected', error='connection_schema_rejected')
        return result
    result.update(schema_valid=True, prediction=value)
    lexical_eligible = (relevance(case['text'], case['thought']) >= MIN_RELEVANCE
                        and len(_tokens(case['text']) - _tokens(case['thought'])) >= 2)
    parsed = _parse_response(raw, SimpleNamespace(summary=case['thought'], current_evidence_index=1),
                             [SimpleNamespace(text=case['thought']), SimpleNamespace(text=case['text'])])
    actual = 'offer' if lexical_eligible and parsed is not None else 'hold'
    acceptable = case['normative']['acceptable_actions']
    result['connection'] = {'actual': actual, 'proposal_action': 'offer' if text else 'hold',
        'acceptable': acceptable, 'accepted': actual in acceptable, 'lexical_eligible': lexical_eligible,
        'parser_accepted': parsed is not None, 'scope': 'offline_parser_and_lexical_scaffold_only',
        'human_ratings': {'relevance': None, 'novelty': None, 'usefulness': None},
        'human_rating_status': 'needed', 'semantic_quality': 'unmeasured'}
    result['normative_pass'] = actual in acceptable
    return result


def summarize(results):
    """Normative precision/recall exclude absent predictions and unlabelled cases.

    A wrong extract on a positive is both FP and FN. Undefined ratios stay null.
    Coverage counts prevent all-error/all-skipped runs from looking successful.
    """
    scored = [r for r in results if r.get('capture', {}).get('status') == 'scored_author_normative']
    tp = sum(r['capture']['outcome'] == 'true_positive' for r in scored)
    fp = sum(r['capture']['false_positive'] for r in scored)
    fn = sum(r['capture']['false_negative'] for r in scored)
    return {'total_cases': len(results),
            'measured_cases': sum(r['status'] in {'measured', 'rejected'} for r in results),
            'not_measured_cases': sum(r['status'] == 'not_measured' for r in results),
            'normative_passes': sum(r.get('normative_pass') is True for r in results),
            'capture_normative': {'scored_cases': len(scored), 'true_positive': tp,
                'false_positive': fp, 'false_negative': fn,
                'incorrect_third_party_ownership': sum(r['capture']['incorrect_third_party_ownership'] for r in scored),
                'precision': tp / (tp + fp) if tp + fp else None,
                'recall': tp / (tp + fn) if tp + fn else None},
            'capture_coverage_complete': bool(scored) and all(
                r.get('capture', {}).get('status') == 'scored_author_normative'
                for r in results if r.get('kind') == 'assessment'),
            'model_accuracy': 'not_established', 'confidence_calibration': 'unmeasured'}


def load_cases(suite='all'):
    paths = ['tests/fixtures/presence_current_turn.json',
             'tests/fixtures/presence_quality_normative.json']
    paths = paths if suite == 'all' else [paths[0 if suite == 'legacy' else 1]]
    cases = []
    for path in paths:
        pack = json.loads((ROOT / path).read_text(encoding='utf-8'))
        for case in pack['cases']:
            cases.append(dict(case, fixture_file=path))
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('duplicate fixture id')
    return cases, paths


def metadata(paths):
    import ast
    import hashlib
    import subprocess
    def digest(path):
        return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    def contract(path, function, prompt_prefix):
        source = (ROOT / path).read_text(encoding='utf-8')
        tree = ast.parse(source)
        schema = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
        prompt = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                      and isinstance(n.value, str) and n.value.startswith(prompt_prefix))
        return (hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                hashlib.sha256(ast.get_source_segment(source, schema).encode('utf-8')).hexdigest())
    current_prompt, current_schema = contract('agent/presence_assessment.py', 'parse', 'Assess only the current human')
    later_prompt, later_schema = contract('agent/presence_initiation.py', '_parse_response', 'Find one useful new connection')
    try:
        revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD', 'HEAD^{tree}'],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=True).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        revision = [None, None]
    return {'candidate_commit': revision[0], 'candidate_committed_tree': revision[1],
        'evaluator_sha256': digest('scripts/eval_presence_current_turn.py'),
        'fixture_hashes': {p: digest(p) for p in paths},
        'prompt_source_hashes': {p: digest(p) for p in ('agent/presence_assessment.py', 'agent/presence_initiation.py')},
        'prompt_hashes': {'current_turn_prompt_utf8': current_prompt, 'later_connection_prompt_utf8': later_prompt},
        'schema_hashes': {'current_turn_parse_source': current_schema, 'later_connection_parse_source': later_schema},
        'source_hash_scope': 'prompt UTF-8 and parser source segments; whole prompt/schema files also pinned; evaluator hash identifies local edits',
        'label_provenance': 'AI-assistant-authored proposals; human review required',
        'legacy_split': 'original scripted prompt/wiring examples; thought labels absent',
        'normative_split': 'heldout candidate, not used to tune prompts; not blind or human validated',
        'broader_languages': 'unverified; legacy Chinese example is not language coverage',
        'confidence': 'self-reported output only, not calibrated probability'}


def not_measured(case, reason):
    return {'id': case['id'], 'kind': case.get('kind', 'assessment'), 'language': case['language'],
        'status': 'not_measured', 'reason': reason, 'error': None, 'schema_valid': None, 'prediction': None,
        'normative_pass': None, 'capture': {'status': 'not_measured'}, 'reserved': False,
        'latency_ms': None, 'physical_requests': 0,
        'output_usage': {'status': 'not_measured', 'output_tokens': None}}


def execute_cases(cases, max_calls):
    """Existing one-shot real entry + shared reservation; no enable/fallback/auth repair.

    No fake ingress/owner is created. Later connection cases require the joined
    authenticated application host, so this standalone CLI never dispatches them.
    The text-only public entry omits usage/authority metadata: report unknown.
    """
    import hashlib
    import time
    from agent.presence_runtime import quiet_hours, clock
    from agent.presence_assessment import messages
    from agent.presence_initiation import _safe_text
    from clover_cli.config import check_config_version, load_config
    from clover_cli.managed_scope import load_managed_config
    from agent.workstreams import load_settings
    from cron.presence import ThoughtStore

    def effective_policy():
        # Same managed/effective policy projection as the runtime, without its
        # startup config migration. Never migrate a profile for an experiment.
        load_managed_config(strict=True)
        config = load_config(migrate=False)
        settings = load_settings(config)
        section = config.get('presence', {})
        enabled = (isinstance(section, dict) and section.get('enabled') is True
                   and settings.active and settings.suggestions_enabled)
        return enabled, config, settings

    results = [not_measured(c, 'max_calls') if c.get('kind') != 'connection' else
               not_measured(c, 'requires_authenticated_later_context_host') for c in cases]
    attempts = invocations = 0
    def remaining(reason, start=0):
        for index in range(start, len(cases)):
            if cases[index].get('kind') != 'connection' and results[index]['status'] == 'not_measured':
                results[index]['reason'] = reason
    try:
        current_version, latest_version = check_config_version()
        if current_version < latest_version:
            remaining('requires_existing_migrated_config')
            return results, attempts, invocations
        enabled, config, settings = effective_policy()
        if not enabled or quiet_hours(config):
            remaining('disabled_or_quiet_hours')
            return results, attempts, invocations
        if min(settings.aux_max_calls_per_event, settings.aux_max_calls_per_day,
               settings.aux_max_output_tokens) <= 0:
            remaining('zero_budget')
            return results, attempts, invocations
        store = ThoughtStore()
        status, generation = store.initiative_status(), store.generation()
        if status[1]:
            remaining('stopped')
            return results, attempts, invocations
    except Exception:
        remaining('policy_or_store_unavailable')
        return results, attempts, invocations

    # Never print endpoint strings, raw configuration, credentials or authority objects.
    task = config.get('auxiliary', {}).get('presence', {})
    identity = {'provider_opaque': hashlib.sha256(json.dumps(
        [config.get('model'), {k: task.get(k) for k in ('provider', 'model', 'base_url', 'api_mode')}],
        sort_keys=True).encode()).hexdigest(), 'authority_opaque': None,
        'authority_status': 'unknown_text_entry_omits_metadata',
        'provider_identity_scope': 'requested_configured_route_fingerprint_not_resolved_account',
        'resolved_provider_opaque': None,
        'transport': 'existing_presence_completion_bounded_entry; adapter detail unknown'}

    for index, case in enumerate(cases):
        if case.get('kind') == 'connection' or attempts >= max_calls:
            continue
        if (case['text'].lstrip().startswith(('/', 'Carry thought:', 'Presence:'))
                or not _safe_text(case['text'], 3072)):
            results[index] = not_measured(case, 'unsafe_or_oversized_input')
            continue
        started = time.monotonic()
        reserved = invoked = False
        try:
            if attempts:
                time.sleep(6)  # Explicit short experiment pacing, not a watcher.
            started = time.monotonic()
            current_version, latest_version = check_config_version()
            if current_version < latest_version:
                remaining('requires_existing_migrated_config', index)
                break
            enabled, fresh_config, fresh_settings = effective_policy()
            if (not enabled or quiet_hours(fresh_config) or fresh_config != config
                    or fresh_settings != settings or store.initiative_status() != status
                    or store.generation() != generation):
                remaining('policy_stop_or_generation_changed', index)
                break
            event = 'quality-eval:' + uuid4().hex
            tokens = min(512, settings.aux_max_output_tokens)
            if not store.reserve_assessment(event_id=event, thought_id=event, revision=1,
                    day=clock().date().isoformat(), max_calls=min(3, settings.aux_max_calls_per_day),
                    tokens=tokens, generation=generation):
                remaining('budget_denied', index)
                break
            reserved = True
            attempts += 1
            async def invoke():
                from agent.auxiliary_client import presence_completion
                return await asyncio.wait_for(presence_completion(messages=messages(case['text']),
                    max_output_tokens=tokens, timeout=5), timeout=5)
            invoked = True
            invocations += 1
            raw = asyncio.run(invoke())
            enabled, fresh_config, fresh_settings = effective_policy()
            if (not enabled or quiet_hours(fresh_config) or fresh_config != config
                    or fresh_settings != settings or store.initiative_status() != status
                    or store.generation() != generation):
                result = not_measured(case, 'late_policy_stop_or_generation_changed')
                remaining('policy_stop_or_generation_changed', index + 1)
            else:
                result = score_case(case, raw)
        except (asyncio.CancelledError, KeyboardInterrupt):
            result = not_measured(case, 'cancelled')
            remaining('cancelled', index + 1)
        except Exception as exc:
            result = not_measured(case, 'provider_or_gate_error')
            result['error'] = type(exc).__name__  # No secret-bearing exception text.
        result.update(reserved=reserved, provider_entry_invoked=invoked,
            latency_ms=round((time.monotonic() - started) * 1000, 3), physical_requests=None if invoked else 0,
            output_usage={'status': 'unknown', 'output_tokens': None}, identity=identity)
        results[index] = result
        if result.get('reason') in {'cancelled', 'late_policy_stop_or_generation_changed'}:
            break
    return results, attempts, invocations


def _reserve_isolated_call(quota_dir, binding, limit):
    """Durably charge before entry. Errors/uncertain completion never refund.

    This is an explicitly consented experiment ledger, NOT the live profile's
    feature policy or daily store. Keep it when resuming the same authorization.
    """
    import fcntl
    import os
    import stat
    from clover_cli.readonly_files import ReadOnlyStateError
    directory = Path(quota_dir)
    info = directory.lstat()
    if (directory.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ReadOnlyStateError('insecure_evaluation_directory')
    path = directory / 'presence-eval-quota.json'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+', encoding='utf-8') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 4096):
            raise ReadOnlyStateError('insecure_evaluation_ledger')
        text = stream.read(4097)
        ledger = json.loads(text) if text else {'version': 1, 'binding': binding, 'used': 0}
        if (set(ledger) != {'version', 'binding', 'used'} or ledger['version'] != 1
                or ledger['binding'] != binding or type(ledger['used']) is not int
                or not 0 <= ledger['used'] <= 3):
            raise ReadOnlyStateError('invalid_evaluation_ledger')
        if ledger['used'] >= min(3, limit):
            return False
        ledger['used'] += 1
        stream.seek(0)
        json.dump(ledger, stream, sort_keys=True)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        return True


def execute_isolated_cases(cases, *, max_calls, quota_dir, request_budget_consent=False):
    """Explicit synthetic experiment, not background presence enablement.

    Select through the supported normal resolver's non-mutating mode. Reuse the
    actual bounded entry/production serializer and prompt with its authority,
    cancellation, one-request/no-fallback contract intact. Request-count consent
    applies ONLY to this isolated run, never edits settings or live budgets.
    Existing live stores require an authenticated host for Stop; do not open or
    inspect their private records here. Every other unsupported state holds.
    """
    import hashlib
    import time
    from copy import deepcopy
    from clover_constants import get_clover_home
    from clover_cli.config import load_config_snapshot, check_config_version
    from clover_cli.managed_scope import load_managed_config
    from clover_cli.runtime_provider import resolve_runtime_provider
    from agent.auxiliary_client import scoped_runtime_main
    from agent.bounded_completion import complete, select_route, CompletionHeld
    from agent.presence_assessment import messages
    from agent.presence_initiation import _safe_text
    from agent.workstreams import load_settings

    results = [not_measured(c, 'max_calls') for c in cases]
    attempts = invocations = 0
    def hold(reason):
        for result in results:
            if result['status'] == 'not_measured' and result['reason'] == 'max_calls':
                result['reason'] = reason
        return results, attempts, invocations
    if request_budget_consent is not True or type(max_calls) is not int or not 1 <= max_calls <= 3:
        return hold('isolated_request_budget_consent_required')
    try:
        def read():
            config = load_config_snapshot()
            current, latest = check_config_version()
            if current < latest:
                raise CompletionHeld('requires_existing_migrated_config')
            managed = load_managed_config(strict=True)
            # Explicit experiment consent is not authority to bypass IT policy.
            if (managed.get('presence', {}).get('enabled') is False
                    or managed.get('proactive', {}).get('enabled') is False):
                raise CompletionHeld('managed_policy_disabled')
            live_store = get_clover_home() / 'cron' / 'presence.db'
            if live_store.exists() or live_store.is_symlink():
                raise CompletionHeld('existing_live_store_requires_authenticated_host')
            if (Path(quota_dir) / 'STOP').exists():
                raise CompletionHeld('stopped')
            return config
        config = read()
        task = config.get('auxiliary', {}).get('presence', {})
        if any(task.get(key) not in (None, '') for key in ('api_key','key_env','api_key_env','key_cmd')):
            return hold('isolated_task_credential_override_forbidden')
        settings = load_settings(config)
        limit = min(3, settings.aux_max_calls_per_day)
        tokens = min(512, settings.aux_max_output_tokens)
        if min(limit, tokens, settings.aux_max_calls_per_event) <= 0:
            return hold('zero_budget')
        runtime = resolve_runtime_provider(read_only=True)
        route = select_route(config)
        if (route.provider != runtime['provider'] or route.model != runtime['model']
                or route.base_url and route.base_url.rstrip('/') != runtime['base_url'].rstrip('/')):
            return hold('isolated_requires_exact_configured_main')
        pool = runtime['credential_pool']
        identity = hashlib.sha256(json.dumps([runtime['provider'], runtime['model'], pool.current().id]).encode()).hexdigest()
        binding = hashlib.sha256(json.dumps([config, identity], sort_keys=True).encode()).hexdigest()
        evaluation = deepcopy(config)
        # Deliberately scoped request-count contract, no claim of token/cost cap.
        evaluation.setdefault('presence', {})['budget_mode'] = 'requests'
    except Exception as exc:
        return hold(getattr(exc, 'reason', 'readonly_selection_unavailable'))
    for index, case in enumerate(cases):
        if attempts >= max_calls:
            break
        if case.get('kind') == 'connection':
            results[index] = not_measured(case, 'requires_authenticated_later_context_host')
            continue
        if (not _safe_text(case['text'], 3072) or case['text'].lstrip().startswith(('/', 'Carry thought:', 'Presence:'))):
            results[index] = not_measured(case, 'unsafe_or_oversized_input')
            continue
        started = time.monotonic()
        reserved = invoked = False
        try:
            if read() != config:
                raise CompletionHeld('configured_route_changed')
            if not _reserve_isolated_call(quota_dir, binding, limit):
                return hold('budget_denied')
            reserved = True
            attempts += 1
            async def invoke():
                return await complete(config=evaluation, messages=messages(case['text']),
                                      max_output_tokens=tokens, timeout=5)
            from clover_cli.readonly_files import non_mutating_config_reads
            with non_mutating_config_reads(), scoped_runtime_main(runtime):
                invoked = True
                invocations += 1
                completion = asyncio.run(invoke())
                completion.authority.validate()
            if read() != config:
                raise CompletionHeld('configured_route_changed')
            result = score_case(case, completion.text)
            result.update(raw_output=completion.text, output_usage={
                'status': completion.usage, 'output_tokens': completion.output_tokens,
                'reasoning_tokens': completion.reasoning_tokens}, selected_identity_opaque=identity,
                configured_model=runtime['model'], budget_contract='isolated_requests_not_server_token_or_cost_cap')
        except (Exception, KeyboardInterrupt, asyncio.CancelledError) as exc:
            result = not_measured(case, getattr(exc, 'reason', 'provider_or_gate_error'))
            result['error'] = type(exc).__name__
        result.update(reserved=reserved, provider_entry_invoked=invoked,
                      physical_requests=None if invoked else 0,
                      latency_ms=round((time.monotonic() - started) * 1000, 3))
        results[index] = result
        if result['status'] == 'not_measured':
            return hold('stopped_after_error_no_retry')
    return results, attempts, invocations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--execute', '--execute-provider-calls', dest='execute', action='store_true')
    modes.add_argument('--execute-isolated', action='store_true',
                       help='Explicit synthetic experiment; never enables live presence or uses live quota.')
    parser.add_argument('--isolated-quota-dir', type=Path)
    parser.add_argument('--acknowledge-request-budget', action='store_true',
                       help='Accept request-count limits, NOT a server token/reasoning/cost cap.')
    modes.add_argument('--replay-scripted', type=Path,
                       help='Offline QA replay JSON: id -> {raw, optional output_tokens}. NOT model results.')
    parser.add_argument('--max-calls', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--suite', choices=('all', 'legacy', 'normative'), default='all')
    args = parser.parse_args(argv)
    if args.execute_isolated and (not args.acknowledge_request_budget or args.isolated_quota_dir is None):
        parser.error('--execute-isolated requires --isolated-quota-dir and --acknowledge-request-budget')
    if not args.execute_isolated and (args.acknowledge_request_budget or args.isolated_quota_dir is not None):
        parser.error('isolated request-budget options require --execute-isolated')
    all_cases, paths = load_cases(args.suite)
    cases = [c for c in all_cases if not args.case or c['id'] in args.case]
    if not cases or set(args.case) - {c['id'] for c in cases}:
        parser.error('unknown or empty case selection')
    results = [not_measured(c, 'dry_run') for c in cases]
    mode, attempts, invocations = 'dry_run', 0, 0
    if args.replay_scripted:
        mode = 'scripted_evaluator_machinery_only'
        try:
            if args.replay_scripted.stat().st_size > 262144:
                raise ValueError('replay bytes')
            replay = json.loads(args.replay_scripted.read_text(encoding='utf-8'))
            if type(replay) is not dict or set(replay) - {c['id'] for c in all_cases}:
                raise ValueError('replay ids')
            results = []
            for case in cases:
                item = replay.get(case['id'])
                if item is None:
                    results.append(not_measured(case, 'missing_scripted_prediction'))
                    continue
                if type(item) is not dict or set(item) - {'raw', 'output_tokens'} or 'raw' not in item:
                    raise ValueError('replay schema')
                usage = item.get('output_tokens')
                if usage is not None and (type(usage) is not int or usage < 0):
                    raise ValueError('replay usage')
                result = score_case(case, item['raw'])
                result.update(reserved=False, physical_requests=0, latency_ms=None,
                    prediction_origin='SCRIPTED_not_model_prediction',
                    output_usage={'status': 'scripted_known' if usage is not None else 'unknown', 'output_tokens': usage})
                results.append(result)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            parser.error('invalid bounded scripted replay file')
    elif args.execute_isolated:
        mode = 'explicit_isolated_request_budget_experiment'
        results, attempts, invocations = execute_isolated_cases(cases, max_calls=args.max_calls,
            quota_dir=args.isolated_quota_dir, request_budget_consent=args.acknowledge_request_budget)
    elif args.execute:
        mode = 'opt_in_experiment'
        results, attempts, invocations = execute_cases(cases, args.max_calls)
    summary = summarize(results)
    report = {'mode': mode, 'available_cases': len(cases), 'max_calls': args.max_calls,
        'attempts_reserved': attempts, 'provider_entry_invocations': invocations,
        'physical_requests': None if invocations else 0,
        'valid_schemas': sum(r['schema_valid'] is True for r in results),
        'normative_posture_matches': sum(r.get('posture', {}).get('accepted') is True for r in results),
        'results': results, 'summary': summary,
        'by_language': {language: summarize([r for r in results if r['language'] == language])
                        for language in sorted({c['language'] for c in cases})},
        'metadata': metadata(paths),
        'notice': 'Author normative comparison only. SCRIPTED runs prove machinery, not model accuracy. '
                  'Missing predictions are NOT measured. Connection semantics need human ratings. '
                  'No calibration, release or full capture/connection certification.'}
    if not args.execute and not args.execute_isolated:
        report['provider_calls'] = 0
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
