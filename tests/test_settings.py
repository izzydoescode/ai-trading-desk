import os

from trading_desk.settings import SKIP_ENV_FILE_VARIABLE, load_env_file


def write_env_file(tmp_path, text):
    """Test helper (not a test): write env file."""
    env_file_path = tmp_path / ".env"
    env_file_path.write_text(text)
    return env_file_path


def test_loads_values_and_reports_names_only(tmp_path, monkeypatch):
    """Verifies: loads values and reports names only."""
    monkeypatch.delenv(SKIP_ENV_FILE_VARIABLE)
    monkeypatch.delenv("DESK_TEST_KEY", raising=False)
    monkeypatch.delenv("DESK_TEST_QUOTED", raising=False)
    env_file_path = write_env_file(
        tmp_path,
        "# comment\n\nDESK_TEST_KEY=abc123\nexport DESK_TEST_QUOTED='quoted value'\nnot a pair\n",
    )
    try:
        assert load_env_file(env_file_path) == ["DESK_TEST_KEY", "DESK_TEST_QUOTED"]
        assert os.environ["DESK_TEST_KEY"] == "abc123"
        assert os.environ["DESK_TEST_QUOTED"] == "quoted value"
    finally:
        os.environ.pop("DESK_TEST_KEY", None)
        os.environ.pop("DESK_TEST_QUOTED", None)


def test_exported_values_win_and_blank_values_are_ignored(tmp_path, monkeypatch):
    """Verifies: exported values win and blank values are ignored."""
    monkeypatch.delenv(SKIP_ENV_FILE_VARIABLE)
    monkeypatch.setenv("DESK_TEST_KEY", "from-shell")
    monkeypatch.delenv("DESK_TEST_BLANK", raising=False)
    env_file_path = write_env_file(tmp_path, "DESK_TEST_KEY=from-file\nDESK_TEST_BLANK=\n")
    assert load_env_file(env_file_path) == []
    assert os.environ["DESK_TEST_KEY"] == "from-shell"
    assert "DESK_TEST_BLANK" not in os.environ


def test_missing_file_and_skip_switch_load_nothing(tmp_path, monkeypatch):
    """Verifies: missing file and skip switch load nothing."""
    monkeypatch.delenv("DESK_TEST_KEY", raising=False)
    env_file_path = write_env_file(tmp_path, "DESK_TEST_KEY=abc123\n")
    assert load_env_file(env_file_path) == []  # skip switch is set by conftest
    monkeypatch.delenv(SKIP_ENV_FILE_VARIABLE)
    assert load_env_file(tmp_path / "absent.env") == []
    assert "DESK_TEST_KEY" not in os.environ
