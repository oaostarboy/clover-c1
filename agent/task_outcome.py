"""Self-reported task outcome, independent of model/transport termination.

This is evidence for the parent to review, never authority to retry tools or
approve work. Unknown is deliberately not a claim of task success.
"""
from __future__ import annotations

import json
import re
from typing import Any

_RESULT = re.compile(r'^Result: (done|not done)$', re.MULTILINE)
_REPORT = re.compile(r'^Task outcome: (\{[^\n]*\})$', re.MULTILINE)


def task_outcome(result: dict[str, Any], summary: str) -> dict[str, Any]:
    unknown: dict[str, Any] = {'status': 'unknown'}
    if result.get('failed') or result.get('error') or result.get('interrupted'):
        return unknown
    raw = result.get('task_outcome')
    if raw is None:
        reports = _REPORT.findall(summary)
        if len(reports) == 1:
            try:
                raw = json.loads(reports[0])
            except ValueError:
                return unknown
        elif reports:
            return unknown  # competing reports are not a reliable verdict
    if raw is not None:
        if not isinstance(raw, dict) or raw.get('status') not in {'done', 'partial', 'blocked'}:
            return unknown
        remaining = raw.get('remaining')
        owner, next_step = raw.get('owner'), raw.get('next_step')
        if (not isinstance(remaining, list) or len(remaining) > 40
                or not all(isinstance(item, str) and 0 < len(item.strip()) <= 2000 for item in remaining)
                or not isinstance(owner, str) or not owner.strip() or len(owner) > 500
                or not isinstance(next_step, str) or not next_step.strip() or len(next_step) > 2000):
            return unknown
        if raw['status'] in {'partial', 'blocked'} and not remaining:
            return unknown
        if raw['status'] == 'done' and remaining:
            return unknown
        # Explicit negative legacy verdict always prevents a success claim.
        if raw['status'] == 'done' and 'not done' in _RESULT.findall(summary):
            return unknown
        return {'status': raw['status'], 'remaining': list(remaining),
                'owner': owner, 'next_step': next_step}
    # Compatibility with the documented single stand-alone Result line, not
    # blanket matching arbitrary prose, tool output, markdown or quoted text.
    verdicts = _RESULT.findall(summary)
    if len(verdicts) == 1:
        return {'status': 'done' if verdicts[0] == 'done' else 'partial'}
    return unknown


def outcome_reason(report: dict[str, Any]) -> str:
    """Human-readable incomplete reason; rendering surfaces still redact it."""
    status = report.get('status', 'unknown')
    if status == 'unknown':
        return 'task success not reported'
    pieces = [status]
    remaining = report.get('remaining')
    if isinstance(remaining, list) and remaining:
        pieces.append('; '.join(remaining))
    if report.get('owner'):
        pieces.append('owner: ' + report['owner'])
    if report.get('next_step'):
        pieces.append('next: ' + report['next_step'])
    return ' — '.join(pieces)
