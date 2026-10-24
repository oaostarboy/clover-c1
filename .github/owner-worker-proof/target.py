"""Produce one explicit test-only next commit without Git replacement refs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def prepare(repo, candidate, tree, caller, evidence, required_ancestor=None):
    assert re.fullmatch('[0-9a-f]{40}', candidate)
    assert re.fullmatch('[0-9a-f]{40}', tree)
    def git(*args, input=None, env=None):
        return subprocess.check_output(['git', '-C', str(repo), *args],
            input=input, text=True, env=env).strip()
    assert git('rev-parse', candidate + '^{tree}') == tree
    if required_ancestor is not None:
        assert re.fullmatch('[0-9a-f]{40}', required_ancestor)
        subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor',
                        required_ancestor, candidate], check=True)
    assert not git('replace', '-l'), 'replacement refs are forbidden'
    assert not git('status', '--porcelain'), 'candidate source must be pristine'
    target, target_tree = candidate, tree
    if caller == 'candidate':
        index = evidence / 'marker.index'
        assert not index.exists()
        env = dict(os.environ, GIT_INDEX_FILE=str(index.resolve()),
                   GIT_AUTHOR_NAME='Native fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='Native fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid',
                   GIT_AUTHOR_DATE='2026-10-23T00:00:00Z', GIT_COMMITTER_DATE='2026-10-23T00:00:00Z')
        git('read-tree', candidate, env=env)
        blob = git('hash-object', '-w', '--stdin', input='owner-worker-next:' + candidate + '\n')
        git('update-index', '--add', '--cacheinfo', '100644', blob,
            '.native-owner-worker-next', env=env)
        target_tree = git('write-tree', env=env)
        target = git('commit-tree', target_tree, '-p', candidate,
            input='test: native owner-worker next marker only\n', env=env)
        assert git('diff', '--name-only', candidate, target) == '.native-owner-worker-next'
        assert git('rev-parse', target + '^') == candidate
        git('update-ref', 'refs/heads/native-proof-next', target)
        index.unlink()
    result = dict(candidate=candidate, candidate_tree=tree, caller=caller,
                  target=target, target_tree=target_tree, marker_only=caller == 'candidate',
                  replacement_refs=False)
    (evidence / 'target.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    evidence = Path(sys.argv[2]).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    result = prepare(Path(sys.argv[1]).resolve(), os.environ['CANDIDATE_SHA'],
                     os.environ['CANDIDATE_TREE'], os.environ['CALLER'], evidence,
                     required_ancestor=os.environ.get('STATUS_LOCK_REPAIR_SHA'))
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as out:
        print('UPDATE_TARGET=' + result['target'], file=out)
        print('UPDATE_TREE=' + result['target_tree'], file=out)
    print(json.dumps(result))
