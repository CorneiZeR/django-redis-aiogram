"""Configuration examples in the docs have to be copy-pasteable.

The README's LOGGING snippet once referenced a `console` handler it never
defined, so anyone pasting it into settings.py got
`ValueError: Unable to configure logger` at startup.
"""

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs" / "wiki").glob("*.md"))]
LOGGING_BLOCK = re.compile(r"^LOGGING = (\{.*?^\})", re.DOTALL | re.MULTILINE)


def logging_examples():
    for path in DOCS:
        if not path.is_file():
            continue
        for match in LOGGING_BLOCK.finditer(path.read_text()):
            yield path.name, match.group(1)


EXAMPLES = list(logging_examples())


def test_there_is_a_logging_example_to_check():
    assert EXAMPLES, "no LOGGING example found in the docs"


@pytest.mark.parametrize("name,source", EXAMPLES, ids=[name for name, _ in EXAMPLES])
def test_every_referenced_handler_is_defined(name, source):
    config = ast.literal_eval(source)
    defined = set(config.get("handlers", {}))
    named = dict(config.get("loggers", {}))
    if "root" in config:  # dictConfig takes the root logger outside 'loggers'
        named["root"] = config["root"]
    for logger, options in named.items():
        missing = set(options.get("handlers", [])) - defined
        assert not missing, f"{name}: logger {logger!r} references undefined handlers {missing}"
