"""Load an exact released post-pull call seam, without executing its updater.

Only the original managed-uv import/call and original outer exception handlers
execute. Fetch/pull, stops, dependency work, and restart bodies are NOT exercised
by this seam. Full positive native update cases cover reachability separately.
"""
import ast
import copy
import hashlib
import re
import subprocess


def load(repo, ref):
    assert re.fullmatch('[0-9a-f]{40}', ref), 'immutable literal caller SHA required'
    filename = 'clover_cli/update_cmd.py'
    raw = subprocess.check_output(['git', '-C', str(repo), 'show', f'{ref}:{filename}'])
    tree = ast.parse(raw.decode('utf-8'), filename=f'git:{ref}:{filename}')
    selected = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for index, statement in enumerate(node.body):
            if (isinstance(statement, ast.ImportFrom) and statement.module == 'clover_cli.managed_uv'
                    and any(alias.name == 'update_managed_uv' for alias in statement.names)):
                selected.append((node, index))
    assert len(selected) == 1, 'exactly one frozen managed-uv call seam required'
    node, index = selected[0]
    call = node.body[index + 1]
    assert (isinstance(call, ast.Expr) and isinstance(call.value, ast.Call)
            and isinstance(call.value.func, ast.Name) and call.value.func.id == 'update_managed_uv'
            and not call.value.args and not call.value.keywords)
    assert not node.orelse and not node.finalbody, 'changed old caller needs explicit fixture review'
    # Retain the actual released handlers; never substitute except Exception.
    seam = ast.Try(body=copy.deepcopy(node.body[index:index+2]),
                   handlers=copy.deepcopy(node.handlers), orelse=[], finalbody=[])
    ast.copy_location(seam, node)
    wrapper = ast.parse('def _frozen_postpull_seam():\n    pass\n').body[0]
    assert isinstance(wrapper, ast.FunctionDef)
    ast.copy_location(wrapper, node)
    wrapper.body = [seam]
    invoke = ast.Expr(value=ast.Call(func=ast.Name(id='_frozen_postpull_seam', ctx=ast.Load()), args=[], keywords=[]))
    ast.copy_location(invoke, call)
    module = ast.fix_missing_locations(ast.Module(body=[wrapper, invoke], type_ignores=[]))
    code = compile(module, f'git:{ref}:{filename}', 'exec')
    return code, dict(caller_sha=ref, source_file=filename,
        source_blob=subprocess.check_output(['git', '-C', str(repo), 'rev-parse', f'{ref}:{filename}'], text=True).strip(),
        source_sha256=hashlib.sha256(raw).hexdigest(), call_line=call.lineno,
        scope='original post-pull import/call and outer handlers only; not a full negative updater run')
