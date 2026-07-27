"""Static checks over the source itself.

These exist because of a concrete failure. The package split moved code
mechanically, and left `run_catchup` referenced but not imported inside
`scheduler_loop`. Nothing caught it: the loop runs forever, so no test calls
it, and it only surfaced as a thread dying in a real run. It also left a
duplicated `save_config` whose store name had been corrupted to
'state.config' -- harmless only because the correct duplicate happened to be
defined second.

Both are exactly what pyflakes reports in a second.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TARGETS = ['taskhome', 'app.py', 'scripts', 'tests']


def run_pyflakes(*paths):
    result = subprocess.run(
        [sys.executable, '-m', 'pyflakes', *paths],
        cwd=REPO, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.mark.parametrize('target', TARGETS)
def test_no_undefined_names_or_dead_imports(target):
    """Catches undefined names, redefinitions and unused imports."""
    output = run_pyflakes(target)
    assert not output, f'pyflakes findings in {target}:\n{output}'


def test_no_function_is_defined_twice_in_a_module():
    """A redefinition silently discards the first version. During the split
    that hid a corrupted store name in the dead copy."""
    import ast
    for path in (REPO / 'taskhome').rglob('*.py'):
        tree = ast.parse(path.read_text())
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, f'{path.name} defines {duplicates} more than once'


def test_importing_the_package_has_no_side_effects():
    """Importing must not read files, start threads or touch hardware -- the
    property that makes the app factory worth having (P0-12)."""
    script = (
        'import threading, sys;'
        'before = threading.active_count();'
        'sys.path.insert(0, %r);'
        'import taskhome;'
        'assert threading.active_count() == before, "a thread was started";'
        'assert taskhome.state.tasks == [], "data was loaded";'
        'print("clean")' % str(REPO)
    )
    result = subprocess.run([sys.executable, '-c', script],
                            cwd='/', capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'clean' in result.stdout
