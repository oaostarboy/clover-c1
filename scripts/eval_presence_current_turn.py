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
                                  capture_output=True, text=True, timeout=3, check=True).stdout.splitlines()
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--execute', '--execute-provider-calls', dest='execute', action='store_true')
    modes.add_argument('--replay-scripted', type=Path,
                       help='Offline QA replay JSON: id -> {raw, optional output_tokens}. NOT model results.')
    parser.add_argument('--max-calls', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--suite', choices=('all', 'legacy', 'normative'), default='all')
    args = parser.parse_args(argv)
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
    if not args.execute:
        report['provider_calls'] = 0
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
