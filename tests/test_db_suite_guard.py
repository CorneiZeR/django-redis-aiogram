"""The database suite refuses the settings module that has no database.

`tests/db/conftest.py` exits early rather than letting a dozen
`ImproperlyConfigured` failures explain the same mistake. Nothing exercised
that, and a guard nothing exercises is a guard that has already stopped
working by the time someone needs it.
"""

import subprocess
import sys

#: seconds a nested interpreter gets before the test fails instead of hanging
SUBPROCESS_TIMEOUT = 120


def test_the_database_suite_refuses_the_settings_module_without_one():
    """Run in a subprocess because the guard's answer is an exit code.

    `-o addopts=''` cancels the `--ignore=tests/db` the default run carries, so
    the directory is collected and the guard is what stops it.

    Bounded, like every subprocess here: a child that hangs would otherwise
    hold the suite until the CI job's own timeout, hours later.
    """
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-o', 'addopts=', '--ds=tests.settings', 'tests/db'],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )

    assert result.returncode == 4, result.stdout + result.stderr
    assert 'run it with --ds=tests.db_settings' in result.stdout + result.stderr
