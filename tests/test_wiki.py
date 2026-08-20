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
    broken = [link for link in LINK.findall(path.read_text(encoding='utf-8')) if target_of(link) not in known]
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
        for link in LINK.findall(path.read_text(encoding='utf-8'))
        if '|' in link and link.split('|')[0].strip() not in known
    ]
    assert not reversed_links, f'{path.name} has label-first links: {reversed_links}'


@pytest.mark.parametrize('path', PAGES, ids=lambda path: path.name)
def test_a_multi_word_page_is_linked_by_its_file_name(path):
    """`[[Sending messages]]` does resolve — GitHub normalises spaces to dashes,
    which is why the check above accepts it — but it names no file, so a rename
    breaks it in a way only a published wiki shows. Both forms were in use here
    at once. Spell the page, let the label carry the spaces.
    """
    loose = [link for link in LINK.findall(path.read_text(encoding='utf-8')) if '|' not in link and ' ' in link.strip()]
    assert not loose, f'{path.name} links to a page by its label: {loose}'


README = ROOT / 'README.md'
PYPROJECT = ROOT / 'pyproject.toml'
# absolute, because the README is also the PyPI description and PyPI does not
# rewrite relative links — ../../wiki/<page> resolves to pypi.org/wiki/<page>
WIKI_URL = 'https://github.com/CorneiZeR/django-redis-aiogram/wiki/'
README_WIKI_LINK = re.compile(rf'\]\({re.escape(WIKI_URL)}([^)#]+)')


def test_readme_wiki_links_resolve():
    """The README links into the wiki with ../../wiki/<page>; a rename there
    breaks them silently."""
    known = page_names()
    targets = README_WIKI_LINK.findall(README.read_text(encoding='utf-8'))
    assert targets, 'no wiki links found in the README'
    broken = [target for target in targets if target not in known]
    assert not broken, f'README links to missing wiki pages: {broken}'


def test_every_documented_log_message_is_still_emitted():
    """The other direction from the table's own purpose.

    `Logging.md` lists the events worth alerting on — a curated subset, not every
    warning the package writes, so requiring the code to be exhausted by it would
    be wrong. What is worth enforcing is the reverse: a row describing a message
    nothing emits any more sends someone to build an alert that can never fire.
    """
    import ast

    page = (ROOT / 'docs' / 'wiki' / 'Logging.md').read_text(encoding='utf-8')
    documented = re.findall(r'^\| `([a-z][^`]+)` \| (?:ERROR|WARNING|INFO) \|', page, re.MULTILINE)
    assert documented, 'no message rows found on the Logging page'

    emitted = []
    for path in sorted((ROOT / 'src' / 'django_redis_aiogram').rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {'debug', 'info', 'warning', 'error', 'exception'}:
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                emitted.append(node.args[0].value)

    stale = [message for message in documented if not any(message in line for line in emitted)]
    assert not stale, f'documented but no longer emitted: {stale}'


def test_wiki_link_syntax_stays_in_the_wiki():
    """`[[Page]]` is wiki-only syntax, and renders literally everywhere else.

    The changelog is read on the repository page and as the package description,
    where a double-bracket link shows its own brackets. It names pages in prose
    instead — "the Deployment page" — and this is what keeps a habit from one
    file leaking into the other.
    """
    outside = {'CHANGELOG.md', 'README.md'}
    offenders = {
        name: [line for line in (ROOT / name).read_text(encoding='utf-8').splitlines() if '[[' in line]
        for name in outside
    }
    broken = {name: lines for name, lines in offenders.items() if lines}
    assert not broken, f'wiki link syntax outside the wiki: {broken}'


CHANGELOG = ROOT / 'CHANGELOG.md'
#: the newest released heading. Everything below it describes what shipped, in the words
#: it shipped with, and is not ours to edit — a spelling pass reached three lines under
#: here and no test noticed
HISTORY_BEGINS = '## 3.0.0 - 2026-08-09'
#: words the released entries spell the way those releases spelled them
HISTORICAL_SPELLINGS = ('`VACUUM` afterwards', 'about its behaviour changed', 'for the old behaviour.')


def test_the_released_entries_keep_the_words_they_shipped_with():
    """A changelog entry is a record of what was said, not prose to be improved.

    The en-US pass for 3.1.0 rewrote three lines below `## 3.0.0` — `afterwards` and two
    `behaviour` — while the pull request describing it said history was left alone. Nothing
    failed, because no test reads down there.

    Narrow on purpose: this pins the words a sweep would reach for, not the whole section,
    so an entry can still be corrected on its facts if one is ever found to be wrong.
    """
    text = CHANGELOG.read_text(encoding='utf-8')
    assert HISTORY_BEGINS in text, f'{HISTORY_BEGINS!r} is gone; this test no longer knows where history starts'
    history = text[text.index(HISTORY_BEGINS) :]
    rewritten = [phrase for phrase in HISTORICAL_SPELLINGS if phrase not in history]

    assert not rewritten, f'a released entry was reworded: {rewritten}'


def test_home_and_sidebar_exist():
    assert (WIKI / 'Home.md').is_file()
    assert (WIKI / '_Sidebar.md').is_file()


def test_the_sidebar_lists_every_page():
    sidebar = (WIKI / '_Sidebar.md').read_text(encoding='utf-8')
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
    text = '## Real\n```\n## Never closed\n[API](https://github.com/CorneiZeR/django-redis-aiogram/wiki/API)\n'

    rendered = visible(text)

    assert sections(rendered) == ['Real']
    assert README_WIKI_LINK.findall(rendered) == []


def test_the_readme_stays_a_front_page():
    lines = README.read_text(encoding='utf-8').splitlines()
    assert len(lines) <= README_BUDGET, f'the README is {len(lines)} lines; anything this long belongs in a wiki page'


def test_no_readme_section_duplicates_a_wiki_page():
    """A section named after a page is that page's material coming back."""
    pages = {normalised(name) for name in page_names()} - {'home', '_sidebar'}
    duplicated = [
        title for title in sections(visible(README.read_text(encoding='utf-8'))) if normalised(title) in pages
    ]

    assert not duplicated, f'these belong in the wiki, not the README: {duplicated}'


def test_the_readme_links_to_every_page():
    """A new page nobody can find from the front page is a page nobody reads.

    Both sides are normalised: GitHub resolves a wiki link case-insensitively and
    treats spaces as dashes, so `../../wiki/rate-limits` reaches the page and has
    to count as reaching it.
    """
    linked = {normalised(target) for target in README_WIKI_LINK.findall(visible(README.read_text(encoding='utf-8')))}
    pages = {normalised(name) for name in page_names()}
    missing = pages - linked - {normalised('Home'), normalised('_Sidebar')}

    assert not missing, f'pages the README does not link to: {sorted(missing)}'


def test_a_link_spelled_the_way_github_accepts_it_counts(tmp_path, monkeypatch):
    """Otherwise the test demands one spelling of a link that has several."""
    readme = tmp_path / 'README.md'
    rows = '\n'.join(f'[{name}]({WIKI_URL}{normalised(name)})' for name in page_names() if name != '_Sidebar')
    readme.write_text(rows + '\n', encoding='utf-8')
    monkeypatch.setattr('tests.test_wiki.README', readme)

    test_the_readme_links_to_every_page()


def a_readme(tmp_path, monkeypatch, body: str):
    """Point the checks at a README of our own, through the name they read."""
    readme = tmp_path / 'README.md'
    rows = '\n'.join(f'[{name}]({WIKI_URL}{normalised(name)})' for name in page_names() if name != '_Sidebar')
    readme.write_text(rows + '\n' + body, encoding='utf-8')
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
    text = readme.read_text(encoding='utf-8')
    row = next(line for line in text.splitlines() if f'{WIKI_URL}webhook)' in line)
    readme.write_text(text.replace(row + '\n', '') + '```\n' + row + '\n```\n', encoding='utf-8')

    with pytest.raises(AssertionError, match='does not link to'):
        test_the_readme_links_to_every_page()


def test_a_link_only_inside_a_comment_does_not_count(tmp_path, monkeypatch):
    readme = a_readme(tmp_path, monkeypatch, '')
    text = readme.read_text(encoding='utf-8')
    row = next(line for line in text.splitlines() if f'{WIKI_URL}api)' in line)
    readme.write_text(text.replace(row + '\n', '') + f'<!-- {row} -->\n', encoding='utf-8')

    with pytest.raises(AssertionError, match='does not link to'):
        test_the_readme_links_to_every_page()


def test_the_readme_has_no_relative_links():
    """The README is also the PyPI long description. PyPI serves it from
    pypi.org and does not rewrite links, so a relative one points at a page
    that does not exist there — which is how 2.0.0 shipped a documentation
    table of dead links.
    """
    relative = re.findall(r'\]\((?!https?://|#)([^)]+)\)', README.read_text(encoding='utf-8'))

    assert not relative, f'relative links break on the PyPI page: {relative}'


def test_the_documentation_url_is_declared():
    """PyPI builds its sidebar from project.urls, not from the description.

    Read as text rather than through tomllib, which the 3.10 floor lacks.
    """
    declared = f'Documentation = "{WIKI_URL.rstrip("/")}"'

    assert declared in PYPROJECT.read_text(encoding='utf-8')
