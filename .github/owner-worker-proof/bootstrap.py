"""Resolve only the explicit binary provided by pinned setup-uv in this CI job."""
from pathlib import Path
import sys

WINDOWS = sys.platform == 'win32'


def resolve(value):
    if not value or not Path(value).is_absolute():
        raise ValueError('proof requires the absolute setup-uv uv-path output')
    candidate = Path(value)
    # setup-uv may expose an absolute extensionless Windows uv-path. Resolve
    # only its adjacent uv.exe; never search PATH or arbitrary PATHEXT entries.
    if WINDOWS and candidate.name.lower() == 'uv' and not candidate.exists():
        candidate = candidate.with_name(candidate.name + '.exe')
    executable = candidate.resolve(strict=True)
    if not executable.is_file():
        raise ValueError('proof bootstrap uv must be a regular executable file')
    return str(executable)
