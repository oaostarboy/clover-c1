"""Offline W4 QA receipt checking, never runtime policy or release approval.

JUnit is local evidence, not authenticated proof of execution. The coordinator
must bind it to commands, exit status and before/after source hashes. This first
slice checks test completeness only; native OS, licensing and independent
review remain separate release gates.
"""
from pathlib import Path
import xml.etree.ElementTree as ET


def inspect_junit(path: Path, *, expected_ids, returncode: int) -> dict:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return {"evidence_passed": False, "release_approved": False,
                "passed": 0, "blockers": ["invalid_junit"]}
    if root.tag not in ("testsuite", "testsuites"):
        return {"evidence_passed": False, "release_approved": False,
                "passed": 0, "blockers": ["invalid_junit"]}
    cases = list(root.iter("testcase"))
    blockers = []
    for suite in root.iter():
        if suite.tag not in ("testsuite", "testsuites"):
            continue
        children = list(suite.iter("testcase"))
        actual = {"tests": len(children),
                  "failures": sum(c.find("failure") is not None for c in children),
                  "errors": sum(c.find("error") is not None for c in children),
                  "skipped": sum(c.find("skipped") is not None for c in children)}
        for field, count in actual.items():
            declared = suite.get(field)
            if declared is not None and (not declared.isascii() or not declared.isdecimal()
                                         or (declared.lstrip("0") or "0") != str(count)):
                blockers.append(f"inconsistent_summary:{field}")
    if type(returncode) is not int or returncode != 0:
        blockers.append("runner_not_successful")
    if (not isinstance(expected_ids, (list, tuple, set, frozenset))
            or any(not isinstance(item, str) or not item for item in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)):
        return {"evidence_passed": False, "release_approved": False,
                "passed": 0, "blockers": ["invalid_expectation"]}
    expected = set(expected_ids)
    if not expected:
        blockers.append("empty_expectation")
    seen = set()
    passed = 0
    for case in cases:
        identity = f"{case.get('classname', '')}.{case.get('name', '')}"
        if identity in seen:
            blockers.append(f"duplicate_testcase:{identity}")
        seen.add(identity)
        if identity not in expected:
            blockers.append(f"unexpected_testcase:{identity}")
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            blockers.append(f"nonpassing_testcase:{identity}")
        else:
            passed += 1
    blockers.extend(f"missing_testcase:{identity}" for identity in sorted(expected - seen))
    return {"evidence_passed": not blockers, "release_approved": False,
            "passed": passed, "blockers": blockers}
