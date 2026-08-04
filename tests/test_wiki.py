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
    target = link.split('|', maxsplit=1)[0]
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
        link for link in LINK.findall(path.read_text()) if '|' in link and link.split('|')[0].strip() not in known
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


#: the README is a front page, not the documentation. It was 351 lines of
#: material the wiki already carried, and it drifted from those pages.
README_BUDGET = 140
#: `## Title`, with the three leading spaces markdown still renders as a heading
ATX = re.compile(r'^ {0,3}#{1,6}\s+(.+?)\s*$', re.MULTILINE)
#: `Title` underlined with `===` or `---`, which renders as one too
SETEXT = re.compile(r'^ {0,3}(\S.*?)\s*\n {0,3}(?:=+|-+)\s*$', re.MULTILINE)
#: a fence opens with at least three of either marker, indented up to three
#: spaces, and runs to a matching close or to the end of the file
FENCE = re.compile(r'^ {0,3}(?P<mark>`{3,}|~{3,}).*?(?:^ {0,3}(?P=mark)[`~]*[ \t]*$|\Z)', re.MULTILINE | re.DOTALL)
COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)


def visible(text: str) -> str:
    """What a reader actually sees.

    A heading inside a fenced block is not a section, and a link inside one is
    not a link — so neither may satisfy the checks below, in either direction.
    """
    return COMMENT.sub('', FENCE.sub('', text))


def sections(text: str) -> list[str]:
    """Every heading a reader sees, in either of the two markdown spellings."""
    return ATX.findall(text) + SETEXT.findall(text)


def normalised(title: str) -> str:
    """`## Rate  limits ##` and `## Rate-limits` name the same page."""
    return '-'.join(re.sub(r'\s*#+\s*$', '', title).split()).lower()


def test_normalised_reads_the_heading_forms_markdown_allows():
    """Written out, because each of these once slipped past the check below."""
    assert normalised('Delivery') == 'delivery'
    assert normalised('Delivery ##') == 'delivery'
    assert normalised('  Rate   limits  ') == 'rate-limits'
    assert normalised('Rate-limits') == 'rate-limits'


def test_visible_drops_what_is_not_rendered():
    text = (
        '## Real\n'
        '```\n## Fenced\n```\n'
        '~~~\n### Tilde fenced\n~~~\n'
        '   ```\n## Indented fence\n   ```\n'
        '````\n## Long marker\n````\n'
        '<!-- ## Commented -->\n'
        '  ### Indented heading\n'
        'Underlined\n=========\n'
    )

    # every heading form counts as a section, and no fence style does
    assert sorted(sections(visible(text))) == ['Indented heading', 'Real', 'Underlined']


def test_an_unclosed_fence_hides_everything_after_it():
    """Which is what GitHub renders: the rest of the file becomes code."""
    text = '## Real\n```\n## Never closed\n[API](../../wiki/API)\n'

    rendered = visible(text)

    assert sections(rendered) == ['Real']
    assert README_WIKI_LINK.findall(rendered) == []


def test_the_readme_stays_a_front_page():
    lines = README.read_text().splitlines()
    assert len(lines) <= README_BUDGET, f'the README is {len(lines)} lines; anything this long belongs in a wiki page'


def test_no_readme_section_duplicates_a_wiki_page():
    """A section named after a page is that page's material coming back."""
    pages = {normalised(name) for name in page_names()} - {'home', '_sidebar'}
    duplicated = [title for title in sections(visible(README.read_text())) if normalised(title) in pages]

    assert not duplicated, f'these belong in the wiki, not the README: {duplicated}'


def test_the_readme_links_to_every_page():
    """A new page nobody can find from the front page is a page nobody reads.

    Both sides are normalised: GitHub resolves a wiki link case-insensitively and
    treats spaces as dashes, so `../../wiki/rate-limits` reaches the page and has
    to count as reaching it.
    """
    linked = {normalised(target) for target in README_WIKI_LINK.findall(visible(README.read_text()))}
    pages = {normalised(name) for name in page_names()}
    missing = pages - linked - {normalised('Home'), normalised('_Sidebar')}

    assert not missing, f'pages the README does not link to: {sorted(missing)}'


def test_a_link_spelled_the_way_github_accepts_it_counts(tmp_path, monkeypatch):
    """Otherwise the test demands one spelling of a link that has several."""
    readme = tmp_path / 'README.md'
    rows = '\n'.join(f'[{name}](../../wiki/{normalised(name)})' for name in page_names() if name != '_Sidebar')
    readme.write_text(rows + '\n')
    monkeypatch.setattr('tests.test_wiki.README', readme)

    test_the_readme_links_to_every_page()


def a_readme(tmp_path, monkeypatch, body: str):
    """Point the checks at a README of our own, through the name they read."""
    readme = tmp_path / 'README.md'
    rows = '\n'.join(f'[{name}](../../wiki/{normalised(name)})' for name in page_names() if name != '_Sidebar')
    readme.write_text(rows + '\n' + body)
    monkeypatch.setattr('tests.test_wiki.README', readme)
    return readme


def test_a_duplicate_heading_inside_a_fence_is_not_a_section(tmp_path, monkeypatch):
    """Driving the check itself, so it fails if it goes back to raw text."""
    a_readme(tmp_path, monkeypatch, '```\n## Delivery\n```\n<!-- ## Troubleshooting -->\n')

    test_no_readme_section_duplicates_a_wiki_page()


def test_a_duplicate_heading_outside_a_fence_is_caught(tmp_path, monkeypatch):
    """The other half: the check must still do its job."""
    a_readme(tmp_path, monkeypatch, '## Delivery\n\nTwo consumers are available.\n')

    with pytest.raises(AssertionError, match='belong in the wiki'):
        test_no_readme_section_duplicates_a_wiki_page()


def test_a_link_only_inside_a_fence_does_not_count(tmp_path, monkeypatch):
    """Reading raw text here would call an unreachable page linked."""
    readme = a_readme(tmp_path, monkeypatch, '')
    text = readme.read_text()
    row = next(line for line in text.splitlines() if '../../wiki/webhook)' in line)
    readme.write_text(text.replace(row + '\n', '') + '```\n' + row + '\n```\n')

    with pytest.raises(AssertionError, match='does not link to'):
        test_the_readme_links_to_every_page()


def test_a_link_only_inside_a_comment_does_not_count(tmp_path, monkeypatch):
    readme = a_readme(tmp_path, monkeypatch, '')
    text = readme.read_text()
    row = next(line for line in text.splitlines() if '../../wiki/api)' in line)
    readme.write_text(text.replace(row + '\n', '') + f'<!-- {row} -->\n')

    with pytest.raises(AssertionError, match='does not link to'):
        test_the_readme_links_to_every_page()
