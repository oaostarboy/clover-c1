"""Resolve only the explicit binary provided by pinned setup-uv in this CI job."""
from pathlib import Path


def resolve(value):
    if not value or not Path(value).is_absolute():
        raise ValueError('proof requires the absolute setup-uv uv-path output')
    executable = Path(value).resolve(strict=True)
    if not executable.is_file():
        raise ValueError('proof bootstrap uv must be a regular executable file')
    return str(executable)
