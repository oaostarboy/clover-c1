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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-provider-calls', action='store_true')
    parser.add_argument('--max-calls', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--case', action='append', default=[])
    args = parser.parse_args(argv)
    pack = json.loads((ROOT / 'tests/fixtures/presence_current_turn.json').read_text(encoding='utf-8'))
    cases = [c for c in pack['cases'] if not args.case or c['id'] in args.case]
    if not cases or set(args.case) - {c['id'] for c in cases}:
        parser.error('unknown or empty case selection')
    if not args.execute_provider_calls:
        print(json.dumps({'mode': 'dry_run', 'available_cases': len(cases),
                          'max_calls': args.max_calls, 'provider_calls': 0,
                          'notice': pack['notice']}))
        return 0

    from agent.presence_runtime import effective_policy, quiet_hours, clock
    from agent.presence_assessment import messages, parse
    from agent.presence_state import AffectReading, EvidenceRef, reading_posture
    from agent.auxiliary_client import presence_completion
    from agent.workstream_origin import text_digest
    from cron.presence import ThoughtStore

    enabled, config, settings = effective_policy()
    if not enabled or quiet_hours(config):
        parser.error('existing opt-in/effective policy or quiet-hours gate denies assessment')
    if min(settings.aux_max_calls_per_event, settings.aux_max_calls_per_day,
           settings.aux_max_output_tokens) <= 0:
        parser.error('existing assessment budget is zero')
    store = ThoughtStore()
    if store.initiative_status()[1]:
        parser.error('profile is stopped')
    attempts = valid = matched = 0
    results = []
    # No private transcript is read. Gold labels never enter the provider prompt.
    for case in cases[:args.max_calls]:
        if attempts:
            import time
            time.sleep(6)  # Explicit experiment pacing, never an ambient watcher.
        enabled, fresh_config, fresh_settings = effective_policy()
        if (not enabled or quiet_hours(fresh_config) or store.initiative_status()[1]
                or fresh_settings != settings):
            break
        event = 'quality-eval:' + uuid4().hex
        tokens = min(512, settings.aux_max_output_tokens)
        if not store.reserve_assessment(event_id=event, thought_id=event, revision=1,
                day=clock().date().isoformat(), max_calls=min(3, settings.aux_max_calls_per_day), tokens=tokens):
            break
        attempts += 1
        try:
            raw = asyncio.run(presence_completion(messages=messages(case['text']),
                max_output_tokens=tokens, timeout=5))
            sample = parse(raw, case['text'])
            value = sample['affect']
            actual = 'abstain'
            if value is not None:
                reading = AffectReading(value['valence'], value['arousal'], value['confidence'], clock(),
                    EvidenceRef('quality_fixture', case['id'], 1, text_digest(case['text'])), value['posture'])
                actual = reading_posture(reading)
            valid += 1
            match = actual == case['expected_posture']
            matched += int(match)
            results.append({'id': case['id'], 'actual': actual, 'expected': case['expected_posture'],
                            'matched_normative_posture': match, 'affect': value})
        except Exception as exc:
            # No fallback or secret-bearing exception text.
            results.append({'id': case['id'], 'held': type(exc).__name__})
    print(json.dumps({'mode': 'opt_in_experiment', 'attempts_reserved': attempts,
        'valid_schemas': valid, 'normative_posture_matches': matched, 'results': results,
        'notice': 'Small selected fixture experiment only; no broad accuracy or confidence calibration claim.'},
        ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
