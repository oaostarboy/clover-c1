"""Literal command identifiers shared by native RPC input boundaries.

Extracted from the selected-source validator without importing shelved features.
Never normalize, coerce, trim or repair a runtime/persisted identifier.
"""
import re


def literal_session_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,512}', value) is not None
