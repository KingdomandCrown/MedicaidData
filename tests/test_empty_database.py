"""Pointing a command at the wrong database.

SQLite creates an empty file for any path it is handed, so a typo in
``--database-url`` does not fail — it succeeds at making a new and useless
database, and the first symptom is a sixty-line traceback ending in "no such
table: npi_ccn_crosswalk". The path is the answer, so the path is what to print.
"""

import pytest

from hospitals.cli import main
from hospitals.db import EmptyDatabase, init_db, make_engine, require_schema


def test_an_empty_database_is_named_rather_than_traced(tmp_path):
    url = f"sqlite:///{tmp_path / 'typo.sqlite'}"
    engine = make_engine(url)

    with pytest.raises(EmptyDatabase) as caught:
        require_schema(engine, url)

    assert "typo.sqlite" in str(caught.value)
    assert "hospitals ingest" in str(caught.value)


def test_a_tiny_sqlite_file_is_called_out_as_probably_a_typo(tmp_path):
    url = f"sqlite:///{tmp_path / 'oops.sqlite'}"
    engine = make_engine(url)
    with engine.connect():
        pass

    with pytest.raises(EmptyDatabase) as caught:
        require_schema(engine, url)

    assert "very likely a typo" in str(caught.value)


def test_a_real_database_passes(tmp_path):
    url = f"sqlite:///{tmp_path / 'real.sqlite'}"
    engine = make_engine(url)
    init_db(engine)

    require_schema(engine, url)  # does not raise


def test_the_cli_exits_cleanly_instead_of_raising(tmp_path, capsys):
    code = main(["link-charges", "--database-url", f"sqlite:///{tmp_path / 'nope.sqlite'}"])

    assert code == 2
    err = capsys.readouterr().err
    assert "nope.sqlite" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    "command",
    [
        ["gap-report", "--database-url"],
        ["price", "85025", "--database-url"],
        ["coverage", "pratt", "--database-url"],
        ["duplicates", "--database-url"],
    ],
)
def test_every_reading_command_says_the_same_thing(command, tmp_path, capsys):
    code = main(command + [f"sqlite:///{tmp_path / 'nope.sqlite'}"])

    assert code == 2
    assert "nope.sqlite" in capsys.readouterr().err


# --- an unset shell variable ----------------------------------------------


def test_an_unset_shell_variable_names_itself_as_the_problem(capsys):
    """`--database-url "$KB"` with KB unset arrives as an empty string.

    SQLAlchemy calls that an unparseable URL, which is true and useless. An
    hour of downloading can finish and then fail at the ingest on this.
    """

    code = main(["link-charges", "--database-url", ""])

    err = capsys.readouterr().err
    assert code == 2
    assert "unset in this shell" in err
    assert "HOSPITALS_DATABASE_URL" in err
    assert "Traceback" not in err


def test_whitespace_is_not_a_database_url():
    with pytest.raises(EmptyDatabase):
        make_engine("   ")


def test_the_default_can_be_named_once_in_the_environment(monkeypatch, tmp_path):
    """A variable that must be re-exported in every new terminal is one an
    hour of work can silently run without."""

    import importlib

    url = f"sqlite:///{tmp_path / 'named.sqlite'}"
    monkeypatch.setenv("HOSPITALS_DATABASE_URL", url)

    import hospitals.cli as cli

    importlib.reload(cli)
    try:
        assert cli.DEFAULT_DB_URL == url
        assert cli.build_parser().parse_args(["stats"]).database_url == url
    finally:
        monkeypatch.delenv("HOSPITALS_DATABASE_URL")
        importlib.reload(cli)


def test_without_the_variable_the_default_is_unchanged(monkeypatch):
    import importlib

    monkeypatch.delenv("HOSPITALS_DATABASE_URL", raising=False)
    import hospitals.cli as cli

    importlib.reload(cli)
    assert cli.DEFAULT_DB_URL == "sqlite:///data/hospitals.sqlite"
