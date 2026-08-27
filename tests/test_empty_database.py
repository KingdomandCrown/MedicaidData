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


# --- charges into a database with no hospitals -----------------------------


def test_charges_are_refused_when_nothing_could_ever_link_them(tmp_path, capsys):
    """38 million rows and ninety minutes of parsing went into the wrong file.

    An unset shell variable fell back to the default path, the load succeeded,
    every row was accepted, and `link-charges` then reported 0 of 118 linked —
    the first sign that anything was wrong, long after the cost was paid.
    """

    from hospitals.db import init_db

    url = f"sqlite:///{tmp_path / 'wrong.sqlite'}"
    init_db(make_engine(url))
    mrf = tmp_path / "a.csv"
    mrf.write_text("description,code|1\n")

    code = main(["ingest-charges", str(mrf), "--database-url", url])

    err = capsys.readouterr().err
    assert code == 2
    assert "holds no hospitals" in err
    assert "HOSPITALS_DATABASE_URL" in err


def test_the_refusal_says_whether_the_variable_is_set(tmp_path, capsys, monkeypatch):
    from hospitals.db import init_db

    url = f"sqlite:///{tmp_path / 'wrong.sqlite'}"
    init_db(make_engine(url))
    mrf = tmp_path / "a.csv"
    mrf.write_text("description,code|1\n")

    monkeypatch.delenv("HOSPITALS_DATABASE_URL", raising=False)
    main(["ingest-charges", str(mrf), "--database-url", url])
    assert "NOT SET" in capsys.readouterr().err

    monkeypatch.setenv("HOSPITALS_DATABASE_URL", "sqlite:///somewhere.sqlite")
    main(["ingest-charges", str(mrf), "--database-url", url])
    assert "set to sqlite:///somewhere.sqlite" in capsys.readouterr().err


def test_a_scratch_database_can_be_asked_for_explicitly(tmp_path, capsys):
    """Refusing outright would block a legitimate charges-before-POS start."""

    from hospitals.db import init_db

    url = f"sqlite:///{tmp_path / 'scratch.sqlite'}"
    init_db(make_engine(url))
    mrf = tmp_path / "a.csv"
    mrf.write_text("description,code|1\n")

    code = main(["ingest-charges", str(mrf), "--database-url", url, "--allow-empty",
                 "--continue-on-error"])
    assert code == 0


def test_a_populated_database_says_which_one_it_is(tmp_path, capsys):
    """The destination is printed before the work, not inferred after it."""

    import datetime as dt

    from sqlalchemy import insert

    from hospitals.db import hospitals as hospitals_table, init_db

    url = f"sqlite:///{tmp_path / 'real.sqlite'}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals_table),
            dict(ccn="170027", name="PRATT", state="KS", is_active=True,
                 ingested_at=dt.datetime(2026, 8, 27)),
        )

    mrf = tmp_path / "a.csv"
    mrf.write_text("description,code|1\n")
    main(["ingest-charges", str(mrf), "--database-url", url, "--continue-on-error"])

    out = capsys.readouterr().out
    assert "real.sqlite" in out
    assert "1 hospitals known" in out
