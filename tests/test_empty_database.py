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
