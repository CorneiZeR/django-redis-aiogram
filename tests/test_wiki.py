"""The wiki is published verbatim, so a broken link ships as a broken link."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / 'docs' / 'wiki'
LINK = re.compile(r'\[\[([^\]]+)\]\]')

PAGES = sorted(WIKI.glob('*.md'))


def page_names() -> set[str]:
    """GitHub turns spaces into dashes when resolving a wiki link."""
    return {path.stem for path in PAGES}


def target_of(link: str) -> str:
    """GitHub wiki links are [[Page-Name|Link Text]] — the page comes first."""
    target = link.split('|')[0]
    return target.strip().replace(' ', '-')


def test_the_wiki_has_pages():
    assert PAGES, 'docs/wiki is empty'


def test_target_of_reads_the_page_not_the_label():
    """Reading the last field instead would have hidden a broken link whose
    label happened to match a page name."""
    assert target_of('Missing|Home') == 'Missing'
    assert target_of('Rate-limits|Rate limits') == 'Rate-limits'
    assert target_of('Home') == 'Home'


@pytest.mark.parametrize('path', PAGES, ids=lambda path: path.name)
def test_every_link_resolves(path):
    known = page_names()
    broken = [link for link in LINK.findall(path.read_text()) if target_of(link) not in known]
    assert not broken, f'{path.name} links to missing pages: {broken}'


@pytest.mark.parametrize('path', PAGES, ids=lambda path: path.name)
def test_piped_links_name_the_page_first(path):
    """[[Page-Name|Link Text]], not the other way round.

    Reversed, GitHub still resolves the page — it normalises spaces to dashes —
    but renders the file name as the label, so the resolve check above cannot
    catch it. This requires the first field to match a page exactly.
    """
    known = page_names()
    reversed_links = [
        link
        for link in LINK.findall(path.read_text())
        if '|' in link and link.split('|')[0].strip() not in known
    ]
    assert not reversed_links, f'{path.name} has label-first links: {reversed_links}'


README = ROOT / 'README.md'
README_WIKI_LINK = re.compile(r'\]\(\.\./\.\./wiki/([^)#]+)')


def test_readme_wiki_links_resolve():
    """The README links into the wiki with ../../wiki/<page>; a rename there
    breaks them silently."""
    known = page_names()
    targets = README_WIKI_LINK.findall(README.read_text())
    assert targets, 'no wiki links found in the README'
    broken = [target for target in targets if target not in known]
    assert not broken, f'README links to missing wiki pages: {broken}'


def test_home_and_sidebar_exist():
    assert (WIKI / 'Home.md').is_file()
    assert (WIKI / '_Sidebar.md').is_file()


def test_the_sidebar_lists_every_page():
    sidebar = (WIKI / '_Sidebar.md').read_text()
    listed = {target_of(link) for link in LINK.findall(sidebar)}
    missing = page_names() - listed - {'_Sidebar'}
    assert not missing, f'pages missing from the sidebar: {sorted(missing)}'
