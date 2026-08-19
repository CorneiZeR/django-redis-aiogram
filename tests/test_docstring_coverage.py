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
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / 'src'
MODULES = sorted(SOURCE.rglob('*.py'))
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def undocumented(tree: ast.Module) -> list[str]:
    """Return `line:qualified.name` for every definition in the tree without a docstring.

    Qualified through the enclosing definitions, because `run` and `send` say nothing on
    their own — the point of the name is to find the closure again.
    """
    found: list[str] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFINITIONS):
                name = f'{prefix}.{child.name}' if prefix else child.name
                if not ast.get_docstring(child):
                    found.append(f'{child.lineno}:{name}')
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

    Write the *why*. A docstring that restates the name satisfies this test and helps
    nobody, and the enclosing docstring is usually the wrong place for a closure's
    reason — `once` is a latch and not a lock, `done` skips cancellation deliberately,
    and neither is guessable from the code around it.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'))

    assert undocumented(tree) == [], f'{path.relative_to(SOURCE)} has undocumented definitions'


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
    the other direction on purpose. It is off, and a custom check asks the same question
    about `src/`. Asserted as text because the suite has no YAML parser — deliberately,
    like the compose snippets in `test_documented_recipes.py`.

    `mode: 'off'` is quoted because YAML 1.1 reads a bare `off` as the boolean false, and
    the schema wants one of three strings — the first version of this config was invalid
    for exactly that reason, and an ignored config means the 80% threshold comes back.
    """
    config = (Path(__file__).resolve().parent.parent / '.coderabbit.yaml').read_text(encoding='utf-8')

    assert "mode: 'off'" in config, 'the unscoped docstring check is back, or its `off` lost its quotes'
    assert 'mode: off\n' not in config, 'a bare `off` parses as false and invalidates the whole config'
    assert 'Docstrings in src' in config, 'nothing asks the scoped question any more'
