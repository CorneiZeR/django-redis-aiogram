"""Everything in `src/` carries a docstring, closures included.

`select = ["ALL"]` already asks ruff for this, and ruff cannot see the whole of it:
pydocstyle's rules apply to *public* module-level and class-level definitions, so a
nested function and a `_private` helper are both invisible to them. That is not a corner
— when this test was written those two categories were the entire gap, eighteen
definitions, and among them the retry loop inside `send_raw`, the callback that decides
whether a killed send is acknowledged, and the thread body that owns the event loop. The
most load-bearing code in the package is written as closures, which is exactly the code
ruff was never going to ask about.

The scan reads the syntax tree, and `test_the_scan_sees_a_nested_definition` is the
control that matters: a walker that quietly stopped descending into function bodies
would report 100% for ever, which is the same false green this file exists to remove.
"""

import ast
import re
from pathlib import Path

import pytest
import yaml

SOURCE = Path(__file__).resolve().parent.parent / 'src'
MODULES = sorted(SOURCE.rglob('*.py'))
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
#: words that carry nothing on their own, so a summary made only of these and the
#: definition's own name has said nothing the signature did not
FILLER = frozenset(
    {
        'a',
        'an',
        'the',
        'of',
        'for',
        'to',
        'and',
        'or',
        'is',
        'it',
        'this',
        'that',
        'its',
        'on',
        'in',
        'with',
        'from',
        'by',
        'as',
        'be',
        'one',
        'are',
        'was',
        'which',
        'what',
        'whether',
        'has',
        'have',
        'return',
        'returns',
        'returning',
    }
)


def undocumented(tree: ast.Module) -> list[str]:
    """Return `line:qualified.name` for every definition in the tree without a docstring.

    Qualified through the enclosing definitions, because `run` and `send` say nothing on
    their own — the point of the name is to find the closure again.
    """
    return [f'{node.lineno}:{name}' for name, node in definitions(tree) if not ast.get_docstring(node)]


def uninformative(tree: ast.Module) -> list[str]:
    """Return the definitions whose docstring only restates their own name."""
    return [
        f'{node.lineno}:{name}'
        for name, node in definitions(tree)
        if (doc := ast.get_docstring(node)) and restates_the_name(node.name, doc)
    ]


def restates_the_name(name: str, docstring: str) -> bool:
    """Whether the summary line says nothing the name did not already say.

    Deliberately conservative — it reports only the degenerate case, a summary whose every
    word is either filler or a word of the name itself. Judging whether a longer docstring
    *earns* its length is not something a test can do, and a check that guessed would be
    worse than none: a false positive here fails the build on a docstring somebody wrote
    on purpose. Run against every definition in `src/` — 484 of them, all documented,
    modules excluded because a separate test covers those — it reports none.
    """
    own = set(re.findall(r'[a-z]+', name.lower()))
    summary = docstring.strip().splitlines()[0]
    said = [
        word
        for word in re.findall(r'[a-z]+', summary.lower())
        if word not in FILLER
        and word not in own
        # a shared stem is the same word: `bucket` against `buckets`, `send` against
        # `sends`. Short words are excluded, where a prefix match is coincidence
        and not any(word.startswith(stem) or stem.startswith(word) for stem in own if len(stem) > 3)
    ]
    return not said


def definitions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Return every definition in the tree, with its qualified name, nesting included."""
    found: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFINITIONS):
                name = f'{prefix}.{child.name}' if prefix else child.name
                found.append((name, child))
                walk(child, name)
            else:
                # a definition can be nested in anything: a `try`, an `if TYPE_CHECKING`,
                # a `with`. Descending only into definitions would miss those
                walk(child, prefix)

    walk(tree, '')
    return found


def test_there_are_modules_to_check():
    """A path that stopped matching would make every case below vacuously true."""
    assert MODULES, f'no sources found under {SOURCE}'


def test_the_scan_sees_a_nested_definition():
    """The control: the gap this test exists for is entirely inside function bodies.

    A walker that did not descend into them would pass the whole package while the
    closures that do the work stayed undocumented, which is the state that shipped for
    three releases.
    """
    tree = ast.parse(
        'def outer():\n'
        '    """Documented."""\n'
        '    def inner():\n'
        '        pass\n'
        '    class Nested:\n'
        '        pass\n'
        '    try:\n'
        '        def guarded():\n'
        '            pass\n'
        '    finally:\n'
        '        pass\n'
    )

    assert undocumented(tree) == ['3:outer.inner', '5:outer.Nested', '8:outer.guarded']


def test_the_scan_accepts_a_documented_nesting():
    """The other direction, so the control cannot pass by reporting everything."""
    tree = ast.parse(
        'def outer():\n'
        '    """Documented."""\n'
        '    def inner():\n'
        '        """Also documented."""\n'
        '        return 1\n'
        '    return inner\n'
    )

    assert undocumented(tree) == []


@pytest.mark.parametrize('path', MODULES, ids=lambda path: str(path.relative_to(SOURCE)))
def test_every_definition_in_the_module_has_a_docstring(path):
    """Not a percentage: one number over a threshold hides which one is missing.

    Existence only: whether a docstring says anything is the next test's question. Write
    the *why* — and note that the enclosing docstring is usually the wrong place for a
    closure's reason, since `once` is a latch rather than a lock and `done` skips
    cancellation deliberately, neither of which is guessable from the code around it.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'))

    assert undocumented(tree) == [], f'{path.relative_to(SOURCE)} has undocumented definitions'


@pytest.mark.parametrize(
    ('name', 'docstring', 'expected'),
    [
        ('retry', 'Retry.', True),
        ('send_message', 'Send the message.', True),
        ('_bucket', 'Return the bucket.', True),
        ('processing_pattern', 'The pattern for processing.', True),
        ('retry', 'Keep calling while Telegram asks for a wait.', False),
        ('_bucket', 'Build a bucket, or None when this limit is switched off.', False),
        ('once', 'Report the first finish and drop every later one.', False),
    ],
)
def test_a_summary_that_only_restates_the_name_is_recognized(name, docstring, expected):
    """Both directions, because a predicate that flagged everything would also pass.

    The negative cases are real docstrings from this package, so a future tightening that
    starts rejecting them fails here rather than in somebody's pull request.
    """
    assert restates_the_name(name, docstring) is expected


@pytest.mark.parametrize('path', MODULES, ids=lambda path: str(path.relative_to(SOURCE)))
def test_no_docstring_in_the_module_only_restates_its_name(path):
    """A docstring that repeats the signature satisfies the existence check and helps nobody.

    `.coderabbit.yaml` asks a reviewer for the broader judgement — whether a docstring
    earns its length. This is the half a test can carry: a summary whose every word is
    filler or a word of the name itself.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'))

    assert uninformative(tree) == [], f'{path.relative_to(SOURCE)} restates a name instead of giving a reason'


@pytest.mark.parametrize('path', MODULES, ids=lambda path: str(path.relative_to(SOURCE)))
def test_every_module_has_a_docstring(path):
    """The file's own reason for existing, which no function docstring carries."""
    assert ast.get_docstring(ast.parse(path.read_text(encoding='utf-8'))), (
        f'{path.relative_to(SOURCE)} does not say what it is for'
    )


def test_the_review_config_asks_this_question_about_src_only():
    """The reviewer-facing half, and the YAML trap that made it silent once.

    The built-in check takes `mode` and `threshold` and nothing else, so it cannot be
    scoped to a path; unscoped it measures test naming, which `pyproject.toml` settles in
    the other direction on purpose. So it is off, and a custom check asks the same
    question about `src/`.

    Parsed rather than grepped, for two reasons the finding that prompted it named: the
    same words in a comment satisfy a text assertion while the active setting is gone, and
    `mode` is where YAML 1.1 bites — a bare `off` is the boolean *false*, which the schema
    rejects outright. The first version of this config was invalid for exactly that
    reason, and an invalid config is an ignored one, which brings the 80% threshold back.
    """
    config = yaml.safe_load((Path(__file__).resolve().parent.parent / '.coderabbit.yaml').read_text('utf-8'))
    checks = config['reviews']['pre_merge_checks']

    mode = checks['docstrings']['mode']
    assert mode == 'off', f'the unscoped docstring check is back on: {mode!r}'
    # the trap, stated as an assertion: unquoted, this is `False` rather than `'off'`
    assert isinstance(mode, str), 'a bare `off` parsed as a boolean, so the whole config is invalid'

    scoped = [check for check in checks['custom_checks'] if check['name'] == 'Docstrings in src']
    assert len(scoped) == 1, 'nothing asks the scoped question any more'
    assert scoped[0]['mode'] == 'warning'
    assert 'src/' in scoped[0]['instructions']
    assert 'tests/' in scoped[0]['instructions'], 'the exclusion has to be stated, not implied'
