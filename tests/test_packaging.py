"""The published tree has to install on the machines it is aimed at.

These are not tests of behaviour. They guard the one thing a test suite
normally cannot see: whether a stranger who clones this repository can run the
first command in the README.

The specific failure they exist to prevent was real. `requirements.txt` was
UTF-8 and used an em dash in a comment. pip reads a requirements file with
`locale.getpreferredencoding(False)` when the file carries no BOM, so on a
Japanese Windows machine -- codepage 932 -- the install died before reading a
single dependency:

    UnicodeDecodeError: 'cp932' codec can't decode byte 0x94 in position 82

CI never saw it. GitHub's windows-latest runner is cp1252, where those bytes
decode to harmless mojibake inside a comment. The failure was invisible to
every check in the pipeline *by construction*, and it triggered on exactly the
audience this project is written for. Passing tests said nothing, because
nothing ran the install on a machine like the reader's.
"""

from __future__ import annotations

import locale
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIREMENTS_FILES = sorted(REPO_ROOT.glob("requirements*.txt"))

# Default codepages of Windows installations in the languages this project is
# most likely to be read in. cp1252 is included deliberately: it is what CI
# runs, and it is the one that hides the problem.
CODEPAGES = ["utf-8", "cp932", "cp949", "gbk", "cp1252"]


def test_requirements_files_were_found():
    """A glob that matches nothing would make every test below vacuous."""
    assert REQUIREMENTS_FILES, "no requirements*.txt found; the guard is not guarding anything"


@pytest.mark.parametrize("path", REQUIREMENTS_FILES, ids=lambda p: p.name)
def test_requirements_are_ascii_only(path: Path):
    """pip has no encoding to go on here, so only ASCII is portable."""
    raw = path.read_bytes()
    offenders = [(i, hex(b)) for i, b in enumerate(raw) if b > 0x7F]
    assert not offenders, (
        f"{path.name} contains non-ASCII bytes at {offenders[:5]}. "
        f"pip decodes this file with the machine's default codepage, so this "
        f"breaks `pip install -r` on any non-UTF-8 locale. Use ASCII "
        f"punctuation in the comments."
    )


@pytest.mark.parametrize("path", REQUIREMENTS_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("codepage", CODEPAGES)
def test_requirements_decode_under_every_common_codepage(path: Path, codepage: str):
    """The direct statement of the property that actually matters."""
    raw = path.read_bytes()
    try:
        raw.decode(codepage)
    except UnicodeDecodeError as exc:
        pytest.fail(
            f"{path.name} cannot be read on a machine whose default encoding is "
            f"{codepage}: {exc}. `pip install -r {path.name}` fails there before "
            f"reading a single dependency."
        )


def test_this_machines_default_encoding_can_read_them():
    """Whatever locale the developer is actually on, it has to work here too."""
    encoding = locale.getpreferredencoding(False)
    for path in REQUIREMENTS_FILES:
        try:
            path.read_bytes().decode(encoding)
        except UnicodeDecodeError as exc:
            pytest.fail(f"{path.name} is unreadable under this machine's {encoding}: {exc}")


def _claimed_test_counts() -> dict[str, list[int]]:
    """Every number either README presents as a test count."""
    import re

    pattern = re.compile(r"(\d{2,4})\s*(?:tests|テスト)")
    found: dict[str, list[int]] = {}
    for name in ("README.md", "README.en.md"):
        path = REPO_ROOT / name
        if path.exists():
            found[name] = [int(m) for m in pattern.findall(path.read_text(encoding="utf-8"))]
    return found


def test_readmes_agree_with_each_other_about_the_test_count():
    """The Japanese and English READMEs drifted apart once already.

    One said 142 while the other still said 127, in a document whose whole
    purpose is to be believed. Numbers maintained by hand in two places go
    stale in one of them; this notices.
    """
    claims = _claimed_test_counts()
    distinct = {n for numbers in claims.values() for n in numbers}
    assert len(distinct) <= 1, f"the READMEs disagree about how many tests there are: {claims}"


def test_readme_test_count_matches_reality():
    """And the number they agree on has to be the true one.

    Collected in a subprocess rather than counted from inside this session,
    because a count taken during the run it is counting is a count of itself.
    """
    import re
    import subprocess
    import sys

    claims = _claimed_test_counts()
    numbers = {n for values in claims.values() for n in values}
    if not numbers:
        pytest.skip("neither README states a test count")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match, f"could not read a count from pytest --collect-only:\n{result.stdout[-500:]}"
    actual = int(match.group(1))

    assert numbers == {actual}, (
        f"the READMEs claim {sorted(numbers)} tests; the suite collects {actual}. "
        f"A number a reader can check in one command is a number worth keeping true."
    )


def test_readme_mcp_tool_count_matches_server():
    """The tool count drifted the very first time a tool was added.

    The READMEs said "8 MCP tools" while server.py registered 10 — found in
    an external re-review 2026-08-12. Same lesson as the test count above:
    a number maintained by hand goes stale the moment the code moves, so
    the machine holds it still.
    """
    import re

    server_src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    actual = len(re.findall(r'name="ledger_', server_src))
    assert actual > 0, "could not count @mcp.tool registrations in server.py"

    pattern = re.compile(r"(\d{1,3})\s*(?:個の MCP ツール|MCP tools)")
    for name in ("README.md", "README.en.md"):
        path = REPO_ROOT / name
        if not path.exists():
            continue
        claims = [int(m) for m in pattern.findall(path.read_text(encoding="utf-8"))]
        assert claims, f"{name} no longer states an MCP tool count; update this test's pattern"
        assert set(claims) == {actual}, (
            f"{name} claims {claims} MCP tools; server.py registers {actual}."
        )


# --- component count -------------------------------------------------------
#
# This repo has already let a component-count claim drift twice (the test
# count above, the MCP tool count above that). "構成要素は 5 つ" became
# stale the moment export_json.py shipped as the 6th. Rather than hardcode
# the expected number here (which would just be a second hand-maintained
# copy of the same fact), this derives it from `[tool.setuptools].py-modules`
# in pyproject.toml — the list that is already the single source of truth
# for which top-level scripts ship — minus `ledger`, which the prose
# explicitly calls out as the shared core rather than one of the wired
# components (see "台帳の本体 (`ledger.py`) は... 依存ゼロのモジュール").


def _shipped_py_modules() -> list[str]:
    """Extract the `py-modules = [...]` list from pyproject.toml.

    Regex, not tomllib: tomllib is stdlib only from Python 3.11, and this
    project's CI matrix (see README) still runs 3.10.
    """
    import re

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert match, "could not find py-modules = [...] in pyproject.toml"
    return re.findall(r'"([^"]+)"', match.group(1))


def _readme_component_table_rows(text: str) -> int:
    """Count component rows in the "構成要素"/component table: lines shaped
    like `| **`name.py`** | ... |`."""
    import re

    return len(re.findall(r"^\|\s*\*\*`[a-z_]+\.py`\*\*\s*\|", text, re.MULTILINE))


_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def test_component_count_matches_shipped_modules_and_both_readmes():
    import re

    py_modules = _shipped_py_modules()
    assert py_modules, "py-modules list came back empty; the regex above needs updating"
    component_modules = [m for m in py_modules if m != "ledger"]
    expected = len(component_modules)

    ja_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    en_text = (REPO_ROOT / "README.en.md").read_text(encoding="utf-8")

    ja_table_rows = _readme_component_table_rows(ja_text)
    en_table_rows = _readme_component_table_rows(en_text)
    assert ja_table_rows == expected, (
        f"README.md's component table has {ja_table_rows} rows; "
        f"pyproject.toml's py-modules (minus ledger) has {expected}: {component_modules}"
    )
    assert en_table_rows == expected, (
        f"README.en.md's component table has {en_table_rows} rows; "
        f"pyproject.toml's py-modules (minus ledger) has {expected}: {component_modules}"
    )

    ja_match = re.search(r"構成要素は\s*(\d+)\s*つ", ja_text)
    assert ja_match, "README.md no longer states '構成要素は N つ'; update this test's pattern"
    assert int(ja_match.group(1)) == expected, (
        f"README.md claims 構成要素は{ja_match.group(1)}つ; actual shipped components: {expected}"
    )

    en_match = re.search(r"(\w+) components,", en_text)
    assert en_match, "README.en.md no longer states 'N components,'; update this test's pattern"
    word = en_match.group(1).lower()
    en_claimed = _NUMBER_WORDS.get(word)
    assert en_claimed is not None, (
        f"README.en.md's component count word {word!r} is not in _NUMBER_WORDS; "
        f"add it or spell out the number differently"
    )
    assert en_claimed == expected, (
        f"README.en.md claims '{word.capitalize()} components'; actual shipped components: {expected}"
    )
