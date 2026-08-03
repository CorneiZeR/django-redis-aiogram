"""The wiki is published verbatim, so a broken link ships as a broken link."""

import re
from pathlib import Path

import pytest

WIKI = Path(__file__).resolve().parent.parent / 'docs' / 'wiki'
LINK = re.compile(r'\[\[([^\]]+)\]\]')

PAGES = sorted(WIKI.glob('*.md'))


def page_names() -> set[str]:
    """GitHub turns spaces into dashes when resolving a wiki link."""
    return {path.stem for path in PAGES}


def target_of(link: str) -> str:
    # [[Label|Target]] or [[Target]]
    target = link.split('|')[-1] if '|' in link else link
    return target.strip().replace(' ', '-')


def test_the_wiki_has_pages():
    assert PAGES, 'docs/wiki is empty'


@pytest.mark.parametrize('path', PAGES, ids=lambda path: path.name)
def test_every_link_resolves(path):
    known = page_names()
    broken = [link for link in LINK.findall(path.read_text()) if target_of(link) not in known]
    assert not broken, f'{path.name} links to missing pages: {broken}'


def test_home_and_sidebar_exist():
    assert (WIKI / 'Home.md').is_file()
    assert (WIKI / '_Sidebar.md').is_file()


def test_the_sidebar_lists_every_page():
    sidebar = (WIKI / '_Sidebar.md').read_text()
    listed = {target_of(link) for link in LINK.findall(sidebar)}
    missing = page_names() - listed - {'_Sidebar'}
    assert not missing, f'pages missing from the sidebar: {sorted(missing)}'
