import os
import shutil

from hospitals.db import charge_sources, make_engine
from hospitals.triage_charges import REVIEW_NOTES_FILE, triage_charges
from sqlalchemy import select

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _drop_folder(tmp_path, files: dict[str, str], name: str = "drop") -> str:
    """A scratch source_dir populated with real files, since triage moves them."""

    source = tmp_path / name
    source.mkdir()
    for name, fixture_name in files.items():
        shutil.copy(os.path.join(FIX, fixture_name), source / name)
    return str(source)


def test_a_good_file_loads_and_moves_to_done(tmp_path):
    source = _drop_folder(tmp_path, {"good.csv": "mrf_tall_sample.csv"})
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"

    summary = triage_charges(source, database_url=db_url)

    assert [s.source_file for s in summary.loaded] == ["good.csv"]
    assert summary.failed == []
    assert not os.path.exists(os.path.join(source, "good.csv"))
    assert os.path.exists(os.path.join(source, "_ingested", "good.csv"))

    engine = make_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(select(charge_sources.c.source_file)).all()
    assert [r[0] for r in rows] == ["good.csv"]


def test_a_file_with_no_data_header_moves_to_review_with_a_reason(tmp_path):
    source = str(tmp_path / "drop")
    os.makedirs(source)
    with open(os.path.join(source, "junk.csv"), "w") as f:
        f.write("a,b,c\n1,2,3\n")
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"

    summary = triage_charges(source, database_url=db_url)

    assert summary.loaded == []
    assert [name for name, _reason in summary.failed] == ["junk.csv"]
    assert not os.path.exists(os.path.join(source, "junk.csv"))
    assert os.path.exists(os.path.join(source, "_needs_review", "junk.csv"))

    notes = open(os.path.join(source, "_needs_review", REVIEW_NOTES_FILE)).read()
    assert "junk.csv" in notes
    assert "data header" in notes


def test_an_unsupported_file_type_moves_to_review_without_being_parsed(tmp_path):
    source = str(tmp_path / "drop")
    os.makedirs(source)
    with open(os.path.join(source, "notes.txt"), "w") as f:
        f.write("just a readme, not price transparency data")
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"

    summary = triage_charges(source, database_url=db_url)

    assert summary.loaded == []
    assert [name for name, _reason in summary.failed] == ["notes.txt"]
    assert os.path.exists(os.path.join(source, "_needs_review", "notes.txt"))
    notes = open(os.path.join(source, "_needs_review", REVIEW_NOTES_FILE)).read()
    assert "not a recognized price-transparency file type" in notes


def test_mixed_batch_sorts_each_file_independently(tmp_path):
    source = _drop_folder(tmp_path, {"good.csv": "mrf_tall_sample.csv"})
    with open(os.path.join(source, "junk.csv"), "w") as f:
        f.write("a,b,c\n1,2,3\n")
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"

    summary = triage_charges(source, database_url=db_url)

    assert len(summary.loaded) == 1
    assert len(summary.failed) == 1
    # The source folder is emptied out — everything moved to one bucket or the other.
    assert os.listdir(source) == ["_ingested", "_needs_review"]


def test_descends_into_round_subfolders(tmp_path):
    """A real drop folder arrives as one subdirectory per download round."""

    source = str(tmp_path / "drop")
    os.makedirs(os.path.join(source, "round 8"))
    os.makedirs(os.path.join(source, "Round 11"))
    shutil.copy(
        os.path.join(FIX, "mrf_tall_sample.csv"),
        os.path.join(source, "round 8", "hospital_a.csv"),
    )
    shutil.copy(
        os.path.join(FIX, "mrf_wide_sample.csv"),
        os.path.join(source, "Round 11", "hospital_b.csv"),
    )
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"

    summary = triage_charges(source, database_url=db_url)

    assert sorted(s.source_file for s in summary.loaded) == [
        "hospital_a.csv", "hospital_b.csv",
    ]
    assert os.path.exists(os.path.join(source, "_ingested", "round 8", "hospital_a.csv"))
    assert os.path.exists(os.path.join(source, "_ingested", "Round 11", "hospital_b.csv"))


def test_underscore_and_dotfile_folders_are_left_alone(tmp_path):
    """A pre-existing "_to_delete" folder (the user's own convention) or a
    dotfile directory is already-handled, not source material to re-walk."""

    source = str(tmp_path / "drop")
    os.makedirs(os.path.join(source, "_to_delete"))
    os.makedirs(os.path.join(source, ".git"))
    shutil.copy(
        os.path.join(FIX, "mrf_tall_sample.csv"),
        os.path.join(source, "_to_delete", "old.csv"),
    )
    shutil.copy(
        os.path.join(FIX, "mrf_tall_sample.csv"),
        os.path.join(source, ".git", "config.csv"),
    )
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"

    summary = triage_charges(source, database_url=db_url)

    assert summary.loaded == []
    assert summary.failed == []
    assert os.path.exists(os.path.join(source, "_to_delete", "old.csv"))
    assert os.path.exists(os.path.join(source, ".git", "config.csv"))


def test_skip_existing_avoids_the_replace_cost_on_a_rerun(tmp_path):
    """A file whose name is already loaded gets skipped outright, not
    re-parsed and replaced -- the expensive path that stalled a real batch."""

    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"
    first_pass = _drop_folder(tmp_path, {"good.csv": "mrf_tall_sample.csv"}, name="drop1")
    triage_charges(first_pass, database_url=db_url)

    # Same filename shows up again in a second drop folder (a re-run over an
    # overlapping round). If skip_existing did not actually skip it, this
    # second pass would replace (delete + re-insert) the original row.
    second_pass = _drop_folder(tmp_path, {"good.csv": "mrf_tall_sample.csv"}, name="drop2")
    engine = make_engine(db_url)
    with engine.begin() as conn:
        before = conn.execute(select(charge_sources.c.id)).scalar_one()

    summary = triage_charges(second_pass, database_url=db_url, skip_existing=True)

    assert summary.loaded == []
    assert summary.skipped == 1
    assert not os.path.exists(os.path.join(second_pass, "good.csv"))
    assert os.path.exists(os.path.join(second_pass, "_ingested", "good.csv"))

    with engine.connect() as conn:
        after = conn.execute(select(charge_sources.c.id)).scalar_one()
    assert after == before  # the original row was never replaced


def test_without_skip_existing_a_repeat_file_still_replaces(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"
    first_pass = _drop_folder(tmp_path, {"good.csv": "mrf_tall_sample.csv"}, name="drop1")
    triage_charges(first_pass, database_url=db_url)

    second_pass = _drop_folder(tmp_path, {"good.csv": "mrf_tall_sample.csv"}, name="drop2")
    summary = triage_charges(second_pass, database_url=db_url)

    assert [s.source_file for s in summary.loaded] == ["good.csv"]
    assert summary.skipped == 0


def test_custom_done_and_review_dirs_are_honoured(tmp_path):
    source = _drop_folder(tmp_path, {"good.csv": "mrf_tall_sample.csv"})
    done = str(tmp_path / "elsewhere_done")
    review = str(tmp_path / "elsewhere_review")
    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"

    triage_charges(source, database_url=db_url, done_dir=done, review_dir=review)

    assert os.path.exists(os.path.join(done, "good.csv"))
    assert not os.path.exists(os.path.join(source, "_ingested"))


def test_retriaging_a_review_folder_folds_back_into_its_siblings(tmp_path):
    """Fixing a parser and re-running against the review folder it produced
    must not nest a fresh _ingested/_needs_review inside itself -- it should
    land in the same two folders the original run made."""

    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"
    source = str(tmp_path / "drop")
    os.makedirs(source)
    with open(os.path.join(source, "junk.csv"), "w") as f:
        f.write("a,b,c\n1,2,3\n")  # force a review-bound failure
    triage_charges(source, database_url=db_url)
    review_dir = os.path.join(source, "_needs_review")
    assert os.path.exists(os.path.join(review_dir, "junk.csv"))

    # Simulate a parser fix: swap in a file that will now succeed, keeping
    # the same name, then re-triage the review folder directly.
    os.remove(os.path.join(review_dir, "junk.csv"))
    shutil.copy(os.path.join(FIX, "mrf_tall_sample.csv"), os.path.join(review_dir, "junk.csv"))
    summary = triage_charges(review_dir, database_url=db_url)

    assert [s.source_file for s in summary.loaded] == ["junk.csv"]
    # Landed in _ingested next to _needs_review, not nested inside it.
    assert os.path.exists(os.path.join(source, "_ingested", "junk.csv"))
    assert not os.path.exists(os.path.join(review_dir, "_ingested"))
    assert not os.path.exists(os.path.join(review_dir, "_needs_review"))


def test_review_notes_file_is_not_treated_as_a_candidate(tmp_path):
    """A re-triage must not re-flag its own bookkeeping file as an
    unrecognized file type on every subsequent run."""

    db_url = f"sqlite:///{tmp_path / 'v.sqlite'}"
    source = str(tmp_path / "drop")
    os.makedirs(source)
    with open(os.path.join(source, "junk.csv"), "w") as f:
        f.write("a,b,c\n1,2,3\n")
    triage_charges(source, database_url=db_url)
    review_dir = os.path.join(source, "_needs_review")
    assert os.path.exists(os.path.join(review_dir, REVIEW_NOTES_FILE))

    summary = triage_charges(review_dir, database_url=db_url)

    assert REVIEW_NOTES_FILE not in [name for name, _reason in summary.failed]
    assert summary.failed == [("junk.csv", summary.failed[0][1])]
