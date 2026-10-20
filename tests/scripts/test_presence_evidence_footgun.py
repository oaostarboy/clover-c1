"""Keep receipt-backed evidence reads distinct from real filesystem encoding I/O."""
from pathlib import Path

from tests.scripts.test_footgun_subprocess_encoding import _load_linter_module


ROOT = Path(__file__).resolve().parents[2]


def test_presence_evidence_readback_does_not_disable_real_path_encoding_checks(tmp_path):
    scanner = _load_linter_module()
    rule = next(rule for rule in scanner.FOOTGUNS
                if rule.name == 'bare Path.read_text()/write_text() without encoding=')
    assert scanner.scan_file(ROOT/'agent/presence_runtime.py', [rule]) == []
    source = tmp_path/'unencoded.py'
    source.write_text('value = path.' + 'read_text()\n', encoding='utf-8')
    assert len(scanner.scan_file(source, [rule])) == 1
    source.write_text('value = path.' + 'read_text(encoding="utf-8")\n', encoding='utf-8')
    assert scanner.scan_file(source, [rule]) == []
