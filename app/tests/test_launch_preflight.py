"""Launch failures must be understandable before dependency imports."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('script,expected', [
    ("import runpy; runpy.run_module('flightmill', run_name='__main__')", 'Missing dependency'),
    ("import sys,runpy; sys.version_info=(3,8,8); "
     "runpy.run_module('flightmill', run_name='__main__')", '64-bit Python 3.12'),
])
def test_launch_preflight_precedes_dependency_imports(script, expected):
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'app/src') + os.pathsep + str(ROOT)}
    result = subprocess.run([sys.executable, '-S', '-c', script, '--help'], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert expected in result.stderr
    assert 'Traceback' not in result.stderr
