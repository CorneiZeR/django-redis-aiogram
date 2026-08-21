"""A library must not log through the root logger.

1.x called `logging.info()` directly, so its messages appeared under whatever
configuration the host project had for the root logger — impossible to route or
silence separately, and gone entirely under `disable_existing_loggers`.

The checks read the syntax tree rather than the text: a regex could not tell
`logging.info(...)` from the same words inside a docstring, and missed
`logging.log(...)`, `import logging as lg` and `from logging import warning`.
"""

import ast
import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / 'src'
MODULES = sorted(SOURCE.rglob('*.py'))
PACKAGE_LOGGER = 'django_redis_aiogram'
# `fatal` and `warn` are deprecated aliases, but they still write to the root
LEVELS = frozenset({'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'fatal', 'log'})
# basicConfig configures the root logger for the whole host process
ROOT_FUNCTIONS = LEVELS | {'basicConfig'}
# `task.exception()` and `warnings.warn()` are not logging, so the receiver has
# to be named like a logger for a level call to mean one
LOGGER_VARIABLES = frozenset({'logger', 'log', 'LOGGER'})


class LoggingUse(ast.NodeVisitor):
    """What one module does with logging."""

    def __init__(self) -> None:
        self.logging_aliases: set[str] = set()  # names bound to the logging module
        self.root_functions: set[str] = set()  # names bound to its root-level functions
        self.getlogger_aliases: set[str] = set()
        self.root_calls: list[str] = []  # offences, as 'line: what'
        self.logger_names: list[tuple[int, str | None]] = []  # what getLogger was asked for
        self.logging_variables: set[str] = set()  # variables called as loggers
        self.package_loggers: set[str] = set()  # variables holding the package logger

    @classmethod
    def read(cls, path: Path) -> 'LoggingUse':
        use = cls()
        use.visit(ast.parse(path.read_text(encoding='utf-8')))
        return use

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == 'logging':
                self.logging_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == 'logging':
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name in ROOT_FUNCTIONS:
                    self.root_functions.add(bound)
                elif alias.name == 'getLogger':
                    self.getlogger_aliases.add(bound)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_package_logger(node.value):
            for target in node.targets:
                # `logger = ...` and `self.logger = ...` both count
                if isinstance(target, ast.Name):
                    self.package_loggers.add(target.id)
                elif isinstance(target, ast.Attribute):
                    self.package_loggers.add(target.attr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            owner, attribute = self._receiver(func.value), func.attr
            if owner in self.logging_aliases and attribute in ROOT_FUNCTIONS:
                self.root_calls.append(f'{node.lineno}: {owner}.{attribute}()')
            elif owner in self.logging_aliases and attribute == 'getLogger':
                self.logger_names.append((node.lineno, self._asked_for(node)))
            elif attribute in LEVELS and owner in LOGGER_VARIABLES:
                self.logging_variables.add(owner)
        elif isinstance(func, ast.Name):
            if func.id in self.root_functions:
                self.root_calls.append(f'{node.lineno}: {func.id}()')
            elif func.id in self.getlogger_aliases:
                self.logger_names.append((node.lineno, self._asked_for(node)))
        self.generic_visit(node)

    def _is_package_logger(self, value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Call)
            and self._asked_for(value) == PACKAGE_LOGGER
            and (
                (
                    isinstance(value.func, ast.Attribute)
                    and value.func.attr == 'getLogger'
                    and isinstance(value.func.value, ast.Name)
                    and value.func.value.id in self.logging_aliases
                )
                or (isinstance(value.func, ast.Name) and value.func.id in self.getlogger_aliases)
            )
        )

    @staticmethod
    def _receiver(value: ast.expr) -> str | None:
        """What a call was made on: `logger`, or the `logger` of `self.logger`."""
        if isinstance(value, ast.Name):
            return value.id
        if isinstance(value, ast.Attribute):
            return value.attr
        return None

    @staticmethod
    def _asked_for(node: ast.Call) -> str | None:
        """The logger name, or None for the root logger / a computed name."""
        # getLogger(name='...') is the same call spelled differently
        given = next(
            (keyword.value for keyword in node.keywords if keyword.arg == 'name'),
            node.args[0] if node.args else None,
        )
        if given is None:
            return None
        if isinstance(given, ast.Constant) and isinstance(given.value, str):
            return given.value
        return ast.unparse(given)


USES = {path: LoggingUse.read(path) for path in MODULES}


def test_there_are_modules_to_check():
    assert MODULES, f'no sources found under {SOURCE}'


def test_the_scan_sees_the_logging_the_package_does():
    """Otherwise every check below would pass over a file it failed to read."""
    with_logger = [path.name for path, use in USES.items() if use.package_loggers]
    logging_somewhere = [path.name for path, use in USES.items() if use.logging_variables]
    assert len(with_logger) >= 3, with_logger
    assert logging_somewhere, 'no module appears to log at all, so nothing is being checked'


@pytest.mark.parametrize('path', MODULES, ids=lambda path: path.name)
def test_no_module_logs_through_the_root_logger(path):
    offences = USES[path].root_calls
    assert not offences, f'{path.name} logs through the root logger at {offences}'


@pytest.mark.parametrize('path', MODULES, ids=lambda path: path.name)
def test_every_logger_asked_for_is_the_package_logger(path):
    """One name everywhere, so a single LOGGING entry routes all of it."""
    wrong = [
        f'{line}: {"getLogger()" if name is None else name!r}'
        for line, name in USES[path].logger_names
        if name != PACKAGE_LOGGER
    ]
    assert not wrong, f'{path.name} asks for another logger at {wrong}'


@pytest.mark.parametrize('path', MODULES, ids=lambda path: path.name)
def test_a_module_that_logs_holds_the_package_logger(path):
    """A module that logs must have bound the package logger itself.

    Logging through a `logger` it never assigned means the messages go somewhere
    this project does not configure."""
    use = USES[path]
    unaccounted = use.logging_variables - use.package_loggers
    assert not unaccounted, f'{path.name} logs through {sorted(unaccounted)}'


# The walk is the thing that must have no blind spot, so it is tested directly:
# `src/` uses module-level loggers, so nothing there would exercise these forms.
SEEN = [
    ("import logging\nlogging.info('x')", 'root_calls'),
    ('import logging\nlogging.log(logging.INFO, "x")', 'root_calls'),
    ("import logging as lg\nlg.warning('x')", 'root_calls'),
    ("from logging import warning\nwarning('x')", 'root_calls'),
    ("import logging\nlogging.fatal('x')", 'root_calls'),
    ('import logging\nlogging.basicConfig()', 'root_calls'),
    ("class Worker:\n    def run(self):\n        self.logger.info('x')", 'logging_variables'),
    ("class Worker:\n    def run(cls):\n        cls.log.error('x')", 'logging_variables'),
]

IGNORED = [
    '"""Never call logging.info() or logging.basicConfig() from here."""\nimport logging',
    'import asyncio\ntask = asyncio.Future()\nerror = task.exception()',
    "import warnings\nwarnings.warn('deprecated')",
]

BOUND = [
    ("import logging\nlogger = logging.getLogger('django_redis_aiogram')", 'logger'),
    ("import logging\nlogger = logging.getLogger(name='django_redis_aiogram')", 'logger'),
    (
        (
            'import logging\nclass Worker:\n    def __init__(self):\n'
            "        self.logger = logging.getLogger('django_redis_aiogram')"
        ),
        'logger',
    ),
    ("from logging import getLogger\nlog = getLogger('django_redis_aiogram')", 'log'),
]

NOT_OURS = [
    'import logging\nlogger = logging.getLogger()',
    'import logging\nlogging.getLogger(__name__)',
]


def walk(source, tmp_path):
    module = tmp_path / 'sample.py'
    module.write_text(source, encoding='utf-8')
    return LoggingUse.read(module)


@pytest.mark.parametrize(('source', 'attribute'), SEEN, ids=[source.splitlines()[-1] for source, _ in SEEN])
def test_the_walk_sees_indirect_logging(source, attribute, tmp_path):
    assert getattr(walk(source, tmp_path), attribute), f'went unnoticed: {source!r}'


@pytest.mark.parametrize('source', IGNORED, ids=lambda source: source.splitlines()[0][:28])
def test_the_walk_ignores_what_is_not_root_logging(source, tmp_path):
    use = walk(source, tmp_path)

    assert use.root_calls == [], source
    assert use.logging_variables == set(), source


@pytest.mark.parametrize(
    ('source', 'expected'), BOUND, ids=['positional', 'keyword', 'on an instance', 'imported getLogger']
)
def test_the_walk_recognizes_the_package_logger_however_it_is_bound(source, expected, tmp_path):
    assert expected in walk(source, tmp_path).package_loggers, source


@pytest.mark.parametrize('source', NOT_OURS, ids=['root logger', 'module name'])
def test_the_walk_refuses_a_logger_that_is_not_ours(source, tmp_path):
    use = walk(source, tmp_path)

    assert use.package_loggers == set(), source
    assert [name for _, name in use.logger_names if name != PACKAGE_LOGGER], source


def _is_logging_call(node: ast.Call, receivers: 'set[str]') -> bool:
    """A call this scan judges: `logger.warning(...)` on a name that holds a logger.

    Levels come from `LEVELS`, the same set the rest of this file uses, rather than a
    second list — mine left out `warn` and `fatal`, which `logging` still accepts, so
    `logger.fatal(msg, **{'extra': ...})` was judged by nothing.

    The *receiver* matters as much as the level: `handler.info('x', extra={'tg_x': 1})`
    has the shape of a logging call and logs nothing, so counting it would fail the
    documentation check over a field nobody emits. `receivers` comes from
    :class:`LoggingUse`, which already tracks which names hold this package's logger.
    """
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in LEVELS:
        return False
    return isinstance(node.func.value, ast.Name) and node.func.value.id in receivers


def _keys_of(mapping: ast.expr, where: str) -> set[str]:
    """The string keys of a literal mapping, refusing anything it cannot read.

    Refusing rather than skipping: a mapping this cannot read is a field that escapes the
    documentation check, which is the one thing the whole scan exists to prevent.
    """
    assert isinstance(mapping, ast.Dict), f'{where}: `extra` is not a literal mapping'
    keys: set[str] = set()
    for key in mapping.keys:
        readable = isinstance(key, ast.Constant) and isinstance(key.value, str)
        assert readable, f'{where}: an `extra=` key this scan cannot read'
        keys.add(key.value)  # type: ignore[union-attr]  # asserted above
    return keys


def _extra_from_dict_call(call: ast.Call, where: str) -> set[str]:
    """`extra=dict(...)`, which is readable — unless it carries a positional mapping."""
    assert not call.args, f'{where}: a positional mapping inside `extra=dict(...)` hides its fields'
    keys: set[str] = set()
    for entry in call.keywords:
        assert entry.arg, f'{where}: a `**` spread inside an `extra=dict(...)`'
        keys.add(entry.arg)
    return keys


def _extra_from_spread(spread: ast.expr, where: str) -> set[str]:
    """A logging call that expands a mapping: `logger.info(msg, **{'extra': {...}})`.

    Its keyword has `arg is None`, so a loop looking for `arg == 'extra'` walks past it
    and the field is never held to the documentation.
    """
    assert isinstance(spread, ast.Dict), f'{where}: a logging call expands a mapping this scan cannot read'
    keys: set[str] = set()
    for key, value in zip(spread.keys, spread.values, strict=True):
        assert isinstance(key, ast.Constant), f'{where}: an unreadable key in an expanded logging call'
        if key.value == 'extra':
            keys |= _keys_of(value, where)
    return keys


def fields_logged_in(source: str, name: str, receivers: 'set[str] | None' = None) -> set[str]:
    """Every `extra=` key one module logs, refusing any shape it cannot read.

    `receivers` defaults to the names this module binds a logger to, which is what makes
    the scan ignore a `handler.info(...)` that happens to share the shape.
    """
    tree = ast.parse(source)
    if receivers is None:
        use = LoggingUse()
        use.visit(tree)
        receivers = use.package_loggers | use.logging_variables
    fields: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_logging_call(node, receivers):
            # `extra=` is only a log field on a logging call: `handler(extra={'tg_x': 1})`
            # is somebody's keyword argument, and judging it would fail this test over a
            # name that is never logged
            continue
        for keyword in node.keywords:
            where = f'{name}:{keyword.value.lineno}'
            if keyword.arg is None:
                fields |= _extra_from_spread(keyword.value, where)
                continue
            if keyword.arg != 'extra':
                continue
            if isinstance(keyword.value, ast.Call):
                by_name = isinstance(keyword.value.func, ast.Name) and keyword.value.func.id == 'dict'
                assert by_name, f'{where}: `extra=` is built by a call this scan cannot read'
                fields |= _extra_from_dict_call(keyword.value, where)
                continue
            fields |= _keys_of(keyword.value, where)
    return fields


def test_every_structured_field_is_documented():
    """The Logging page exists to say what these mean, and it had fallen ten
    fields behind — every one of them added by a release that documented the
    feature and not the field it logs.

    Both directions: a field the page describes and nothing emits is a reader
    looking for something that will never appear.

    Read from the keys of `extra=` mappings rather than from every `tg_`-prefixed string
    in the package: taking every constant let `MODULE_NAME`'s default, the string
    `'tg_router'`, count as a field, and the page kept a row for a field nothing has ever
    written. Every shape that could hide a key fails rather than being skipped — a
    computed key, `dict(mapping)`, and a `**` expansion on a logging call, whose keyword
    has no name at all.
    """
    emitted: set[str] = set()
    for path in MODULES:
        emitted |= fields_logged_in(path.read_text(encoding='utf-8'), path.name)
    page = (SOURCE.parent / 'docs' / 'wiki' / 'Logging.md').read_text(encoding='utf-8')
    documented = set(re.findall(r'`(tg_[a-z_]+)`', page))

    assert emitted - documented == set(), f'undocumented log fields: {sorted(emitted - documented)}'
    assert documented - emitted == set(), f'documented but never logged: {sorted(documented - emitted)}'


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ("logger.info('x', extra={'tg_a': 1})", {'tg_a'}),
        ("logger.info('x', extra=dict(tg_b=1))", {'tg_b'}),
        ("logger.info('x', **{'extra': {'tg_c': 1}})", {'tg_c'}),
        ("logger.info('x', **{'extra': {'tg_d': 1}, 'stacklevel': 2})", {'tg_d'}),
        ('self.handler(**call)', set()),
        ("handler(extra={'tg_value': 1})", set()),
        # the shape of a logging call on something that is not a logger: `handler.info`
        # logs nothing, so counting its `extra` would fail the documentation check over a
        # field nobody emits
        ("handler.info('x', extra={'tg_unrelated': 1})", set()),
        ("self.logger.info('x', extra={'tg_attribute': 1})", set()),
        ("logger.fatal('x', **{'extra': {'tg_f': 1}})", {'tg_f'}),
        ("logger.warn('x', extra={'tg_w': 1})", {'tg_w'}),
    ],
)
def test_the_field_scan_reads_every_shape_it_should(source, expected):
    """The scan above is only as good as the shapes it can read.

    A `**` expansion on a logging call is the one that motivated this: its keyword has no
    name, so a loop matching `arg == 'extra'` walks straight past it. `**call` on
    something that is not a logging call stays allowed, because that is ordinary code.
    """
    assert fields_logged_in(source, 'probe.py', receivers={'logger'}) == expected


@pytest.mark.parametrize(
    ('source', 'refusal'),
    [
        ("logger.info('x', **payload)", 'expands a mapping this scan cannot read'),
        ("logger.info('x', extra=fields)", 'not a literal mapping'),
        ("logger.info('x', extra=dict(fields, tg_e=1))", 'positional mapping'),
        ("logger.info('x', **{'extra': fields})", 'not a literal mapping'),
        ("logger.info('x', extra=build())", 'built by a call this scan cannot read'),
    ],
)
def test_the_field_scan_refuses_what_it_cannot_read(source, refusal):
    """Skipping an unreadable shape is how a field escapes the documentation check.

    Each of these once passed silently, one shape at a time, and each was found by review
    rather than by this suite — so the refusals are pinned rather than trusted.
    """
    with pytest.raises(AssertionError, match=refusal):
        fields_logged_in(source, 'probe.py', receivers={'logger'})
