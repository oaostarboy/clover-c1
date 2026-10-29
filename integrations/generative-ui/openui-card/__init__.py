"""Standalone OpenUI-compatible bounded UI component producer.

Consumes Clover's native StructuredToolSpec boundary.
Projects clover.openui.comparison v1 records compatible with OpenUI Lang.
No actions, sends, URLs, HTML, or cloud telemetry.
"""
import json

from tools.structured_input import StructuredToolSpec

NAME = "openui_card"
COMPONENT = "clover.openui.comparison"
VERSION = 1
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "title", "columns", "rows"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "columns": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
        },
        "rows": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "values"],
                "properties": {
                    "label": {"type": "string", "minLength": 1, "maxLength": 120},
                    "values": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {"type": "string", "maxLength": 500},
                    },
                },
            },
        },
        "openui_lang": {"type": "string", "minLength": 1, "maxLength": 4096},
    },
}


def _text(value, maximum, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError("text bounds")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("control character")
    value.encode("utf-8")  # reject lone surrogates


def validate(value):
    allowed_keys = {"version", "title", "columns", "rows", "openui_lang"}
    if not set(value).issubset(allowed_keys) or not {"version", "title", "columns", "rows"}.issubset(set(value)):
        raise ValueError("closed comparison schema")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("version")
    _text(value["title"], 160)
    columns, rows = value["columns"], value["rows"]
    if not isinstance(columns, list) or not 2 <= len(columns) <= 4:
        raise ValueError("column bounds")
    for column in columns:
        _text(column, 80)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("row bounds")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"label", "values"}:
            raise ValueError("closed row schema")
        _text(row["label"], 120)
        values = row["values"]
        if not isinstance(values, list) or len(values) != len(columns):
            raise ValueError("row width")
        for text in values:
            _text(text, 500, 0)
    if "openui_lang" in value:
        _text(value["openui_lang"], 4096)


def result_status(value):
    if value == {"status": "success", "component": COMPONENT, "version": VERSION}:
        return "success"
    if value.get("status") == "cancelled":
        return "cancelled"
    return "error"


def project(value):
    out = {key: value[key] for key in ("version", "title", "columns", "rows")}
    if "openui_lang" in value:
        out["openui_lang"] = value["openui_lang"]
    return out


SPEC = StructuredToolSpec(
    component=COMPONENT,
    version=VERSION,
    validate=validate,
    project=project,
    result_status=result_status,
    max_bytes=32768,
    max_depth=6,
)


def handler(args, **_kwargs):
    validate(args)
    return json.dumps({"status": "success", "component": COMPONENT, "version": VERSION})


def register(ctx):
    ctx.register_tool(
        name=NAME,
        toolset="openui",
        description="Present a read-only OpenUI-compatible comparison component.",
        schema={
            "name": NAME,
            "description": "Present a read-only comparison without performing actions.",
            "parameters": SCHEMA,
        },
        handler=handler,
        check_fn=lambda: True,
        structured_input=SPEC,
    )
