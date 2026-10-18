"""Synthetic evidence fixtures for the offline W4 receipt checker."""
import pytest

try:
    from qa.presence import receipt_gate
except ImportError:
    receipt_gate = None


def inspect(tmp_path, xml, *, expected_ids=("synthetic.test_ok",), returncode=0):
    assert receipt_gate is not None, "W4 has no fail-closed JUnit receipt checker"
    path = tmp_path / "synthetic.xml"
    path.write_text(xml, encoding="utf-8")
    return receipt_gate.inspect_junit(path, expected_ids=expected_ids, returncode=returncode)


def test_complete_synthetic_receipt_verifies_tests_but_never_approves_release(tmp_path):
    result = inspect(tmp_path, '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
                     '<testcase classname="synthetic" name="test_ok"/>'
                     '</testsuite></testsuites>')
    assert result == {"evidence_passed": True, "release_approved": False,
                      "passed": 1, "blockers": []}


@pytest.mark.parametrize("status", ["failure", "error", "skipped"])
def test_summary_cannot_hide_nonpassing_synthetic_testcase(tmp_path, status):
    result = inspect(tmp_path, '<testsuite tests="1" failures="0" errors="0" skipped="0">'
                     f'<testcase classname="synthetic" name="test_ok"><{status}/></testcase>'
                     '</testsuite>')
    assert result["evidence_passed"] is False
    assert result["release_approved"] is False
    assert result["passed"] == 0
    assert "nonpassing_testcase:synthetic.test_ok" in result["blockers"]


@pytest.mark.parametrize("cases,expected_ids,blocker", [
    ('<testcase classname="synthetic" name="test_ok"/>', (), "empty_expectation"),
    ('', ("synthetic.test_ok",), "missing_testcase:synthetic.test_ok"),
    ('<testcase classname="synthetic" name="test_other"/>', ("synthetic.test_ok",),
     "unexpected_testcase:synthetic.test_other"),
    ('<testcase classname="synthetic" name="test_ok"/>' * 2, ("synthetic.test_ok",),
     "duplicate_testcase:synthetic.test_ok"),
])
def test_synthetic_receipt_requires_exact_nonempty_test_identity_manifest(
        tmp_path, cases, expected_ids, blocker):
    result = inspect(tmp_path, f'<testsuite>{cases}</testsuite>', expected_ids=expected_ids)
    assert result["evidence_passed"] is False
    assert result["release_approved"] is False
    assert blocker in result["blockers"]


@pytest.mark.parametrize("returncode", [1, 5, -9, None, False, "0"])
def test_synthetic_green_xml_cannot_override_unsuccessful_or_invalid_runner_exit(tmp_path, returncode):
    result = inspect(tmp_path, '<testsuite><testcase classname="synthetic" name="test_ok"/></testsuite>',
                     returncode=returncode)
    assert result["evidence_passed"] is False
    assert "runner_not_successful" in result["blockers"]


@pytest.mark.parametrize("xml", ["", "<testsuite>", "<not-junit><testcase "
                                 'classname="synthetic" name="test_ok"/></not-junit>'])
def test_unreadable_or_non_junit_synthetic_evidence_holds_instead_of_raising(tmp_path, xml):
    result = inspect(tmp_path, xml)
    assert result["evidence_passed"] is False
    assert result["release_approved"] is False
    assert result["passed"] == 0
    assert "invalid_junit" in result["blockers"]


@pytest.mark.parametrize("field,value", [("tests", "2"), ("tests", "junk"), ("tests", "-1"),
                                        ("failures", "1"), ("errors", "1"), ("skipped", "1")])
def test_synthetic_suite_totals_must_match_real_testcases(tmp_path, field, value):
    result = inspect(tmp_path, f'<testsuite {field}="{value}">'
                     '<testcase classname="synthetic" name="test_ok"/></testsuite>')
    assert result["evidence_passed"] is False
    assert f"inconsistent_summary:{field}" in result["blockers"]


@pytest.mark.parametrize("expected_ids", [None, "synthetic.test_ok", [None],
                                         [""], ["synthetic.test_ok"] * 2])
def test_invalid_synthetic_expectation_manifest_holds_instead_of_raising(tmp_path, expected_ids):
    result = inspect(tmp_path, '<testsuite><testcase classname="synthetic" name="test_ok"/></testsuite>',
                     expected_ids=expected_ids)
    assert result["evidence_passed"] is False
    assert "invalid_expectation" in result["blockers"]


def test_oversized_synthetic_count_is_a_blocker_not_an_integer_conversion_crash(tmp_path):
    result = inspect(tmp_path, '<testsuite tests="' + '9' * 5000 + '">'
                     '<testcase classname="synthetic" name="test_ok"/></testsuite>')
    assert result["evidence_passed"] is False
    assert "inconsistent_summary:tests" in result["blockers"]
