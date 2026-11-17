"""Guards on the test suite itself.

A test without an assertion passes whatever the code does, so it proves nothing.
These checks read the test files and fail the build if one slips in, and keep
builder helpers named so nobody mistakes them for tests.
"""

import ast
from pathlib import Path

import pytest

TESTS_DIRECTORY = Path(__file__).parent
TEST_FILES = sorted(TESTS_DIRECTORY.glob("test_*.py"))
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
# Helpers that do something (run, fetch, wait) are named for the action. Helpers that
# only build a value must say so, so they cannot be mistaken for a test.
BUILDER_PREFIXES = ("make_", "fake_")
ACTION_HELPERS = {
    "backtest",
    "check",
    "client",
    "decide",
    "decide_reversion",
    "decide_short",
    "drain",
    "evaluate",
    "events_of",
    "fetch_with_handler",
    "hold_flat_and_losing",
    "no_sleep",
    "notes_for",
    "open_short",
    "parse_resolved",
    "run",
    "run_cli_in_process",
    "short_real_wait",
    "signal_kinds",
    "slow_client",
    "definitions_in",
    "top_level_functions",
    "trace_entry",
    "verifies_something",
    "wait_for_status",
    "write_env_file",
    "yield_once",
}


def top_level_functions(test_file: Path) -> list[FunctionNode]:
    """Test helper (not a test): top level functions."""
    return [
        node for node in ast.parse(test_file.read_text()).body if isinstance(node, FunctionNode)
    ]


def verifies_something(test_function: FunctionNode) -> bool:
    """Test helper (not a test): true when the test asserts, or expects an exception with
    `pytest.raises`.
    """
    for node in ast.walk(test_function):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Attribute) and node.attr in {"raises", "fail"}:
            return True
    return False


def test_the_suite_is_large_enough_for_these_guards_to_mean_something():
    """Verifies: the suite is large enough for these guards to mean something."""
    test_functions = [
        function
        for test_file in TEST_FILES
        for function in top_level_functions(test_file)
        if function.name.startswith("test_")
    ]
    assert len(TEST_FILES) >= 25
    assert len(test_functions) >= 300


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda test_file: test_file.name)
def test_every_test_function_makes_at_least_one_assertion(test_file):
    """Verifies: every test function makes at least one assertion."""
    tests_without_assertions = [
        function.name
        for function in top_level_functions(test_file)
        if function.name.startswith("test_") and not verifies_something(function)
    ]
    assert tests_without_assertions == []


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda test_file: test_file.name)
def test_helpers_that_build_values_are_named_as_builders(test_file):
    """Verifies: helpers that build values are named as builders."""
    ambiguous_helpers = [
        function.name
        for function in top_level_functions(test_file)
        if not function.name.startswith(("test_", *BUILDER_PREFIXES))
        and function.name not in ACTION_HELPERS
    ]
    assert ambiguous_helpers == []


def test_the_assertion_guard_catches_an_empty_test_and_accepts_real_ones():
    """Verifies: the assertion guard catches an empty test and accepts real ones."""
    empty, asserting, raising = ast.parse(
        "def test_empty():\n    make_account_snapshot()\n"
        "def test_asserting():\n    assert 1 + 1 == 2\n"
        "def test_raising():\n    with pytest.raises(ValueError):\n        int('x')\n"
    ).body
    assert not verifies_something(empty)
    assert verifies_something(asserting)
    assert verifies_something(raising)


SOURCE_FILES = sorted((TESTS_DIRECTORY.parent / "src" / "trading_desk").rglob("*.py"))
# A function this long cannot be understood at a glance, so it must explain itself.
LONGEST_UNDOCUMENTED_DEFINITION_LINES = 12
HELPER_LABELS = ("Test helper", "Test fixture", "Test double")


def definitions_in(tree: ast.AST):
    """Test helper (not a test): every function and class in a parsed file, however nested."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda test_file: test_file.name)
def test_every_test_says_what_it_verifies_and_every_helper_is_labelled(test_file):
    """Verifies: each test has a docstring stating what it checks, and each helper, fixture
    or test double in a test file says in its docstring that it is not a test.
    """
    undocumented_tests, unlabelled_helpers = [], []
    for node in ast.parse(test_file.read_text()).body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        docstring = ast.get_docstring(node)
        if node.name.startswith("test_"):
            if not docstring:
                undocumented_tests.append(node.name)
        elif not (docstring and docstring.startswith(HELPER_LABELS)):
            unlabelled_helpers.append(node.name)
    assert undocumented_tests == []
    assert unlabelled_helpers == []


@pytest.mark.parametrize(
    "source_file",
    SOURCE_FILES,
    ids=lambda source_file: source_file.relative_to(TESTS_DIRECTORY.parent).as_posix(),
)
def test_every_long_function_and_class_in_the_source_explains_itself(source_file):
    """Verifies: no function or class longer than a dozen lines lacks a docstring, so a
    reader can learn what it does without reading its body.
    """
    undocumented = [
        f"{node.name} ({node.end_lineno - node.lineno + 1} lines)"
        for node in definitions_in(ast.parse(source_file.read_text()))
        if node.end_lineno - node.lineno + 1 > LONGEST_UNDOCUMENTED_DEFINITION_LINES
        and ast.get_docstring(node) is None
    ]
    assert undocumented == []
