"""The database suite refuses the settings module that has no database.

`tests/db/conftest.py` exits early rather than letting a dozen
`ImproperlyConfigured` failures explain the same mistake. Nothing exercised
that, and a guard nothing exercises is a guard that has already stopped
working by the time someone needs it.
"""

import subprocess
import sys


def test_the_database_suite_refuses_the_settings_module_without_one():
    """Run in a subprocess because the guard's answer is an exit code.

    `-o addopts=''` cancels the `--ignore=tests/db` the default run carries, so
    the directory is collected and the guard is what stops it.
    """
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-o', 'addopts=', '--ds=tests.settings', 'tests/db'],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4, result.stdout + result.stderr
    assert 'run it with --ds=tests.db_settings' in result.stdout + result.stderr
