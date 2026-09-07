"""Getting an assessment into the database, and attributing it to a hospital.

The attribution rules matter more than the loading. A CHNA filed against the
wrong hospital puts another community's priorities on a scorecard, and there
is no downstream symptom -- the hospital simply appears to have said something
it never said.
"""

import datetime as dt

import pytest
from sqlalchemy import insert, select

from hospitals.chna_load import attribute, load_assessment, load_path
from hospitals.db import (
    chna_documents,
    chna_needs,
    chna_shortages,
    hospitals,
    init_db,
    make_engine,
)
from tests.test_chna import CHEYENNE, PATTERSON

NOW = dt.datetime(2026, 9, 6)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 't.sqlite'}")
    init_db(eng)
    return eng


def _hospital(engine, ccn, name, state="KS"):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            dict(ccn=ccn, name=name, state=state, is_active=True, ingested_at=NOW),
        )


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- attribution ------------------------------------------------------------


def test_a_document_is_attributed_by_name_and_state(engine, tmp_path):
    _hospital(engine, "170027", "PATTERSON HEALTH CENTER")
    result = load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    assert result.ccn == "170027"
    assert result.status == "loaded"


def test_a_hospital_in_another_state_is_not_a_match(engine, tmp_path):
    _hospital(engine, "260027", "PATTERSON HEALTH CENTER", state="MO")
    result = load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    assert result.ccn is None
    assert result.status == "unattributed"


def test_two_hospitals_sharing_a_name_are_not_resolved_by_a_coin_flip():
    from hospitals.chna import parse_assessment
    from hospitals.link import normalize_name

    roster = {(normalize_name("Patterson Health Center"), "KS"): {"170027", "170099"}}

    ccn, status = attribute(parse_assessment(PATTERSON), roster)

    assert ccn is None
    assert status == "ambiguous"


def test_a_document_naming_no_hospital_is_loaded_unattributed(engine, tmp_path):
    result = load_assessment(engine, _write(tmp_path, "x.txt", "Some other report."))

    assert result.ccn is None
    assert result.status == "unattributed"


def test_an_unattributed_document_is_still_stored(engine, tmp_path):
    """The data is in hand; only the join is missing."""

    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    with engine.connect() as conn:
        rows = conn.execute(select(chna_documents.c.source_file)).all()
    assert len(rows) == 1


# --- what gets written ------------------------------------------------------


def test_both_ranked_tables_are_stored_and_kept_apart(engine, tmp_path):
    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    with engine.connect() as conn:
        kinds = [r.table_kind for r in conn.execute(select(chna_needs.c.table_kind))]

    assert kinds.count("priority") == 6
    assert kinds.count("ongoing") == 5


def test_the_previous_rank_survives_the_round_trip(engine, tmp_path):
    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    with engine.connect() as conn:
        row = conn.execute(
            select(chna_needs.c.prior_rank).where(chna_needs.c.label == "Quality Housing")
        ).one()

    assert row.prior_rank == 5


def test_a_recovered_row_is_still_flagged_in_the_database(engine, tmp_path):
    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    with engine.connect() as conn:
        row = conn.execute(
            select(chna_needs.c.recovered, chna_needs.c.votes)
            .where(chna_needs.c.label == "Home Health")
        ).one()

    assert row.recovered is True
    assert row.votes == 7


def test_the_town_hall_details_are_kept(engine, tmp_path):
    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    with engine.connect() as conn:
        doc = conn.execute(select(chna_documents)).mappings().one()

    assert doc["attendees"] == 27
    assert doc["total_votes"] == 108
    assert doc["cycle_label"] == "Round #5"
    assert doc["consultant"] == "vvv"


def test_a_shortage_is_stored_unconfirmed(engine, tmp_path):
    """It came from a heuristic, and it must not look like a fact."""

    load_assessment(engine, _write(tmp_path, "c.txt", CHEYENNE))

    with engine.connect() as conn:
        rows = conn.execute(
            select(chna_shortages.c.specialty, chna_shortages.c.confirmed)
        ).all()

    assert rows
    assert all(r.confirmed is False for r in rows)
    assert "urology" in {r.specialty for r in rows}


# --- reloading --------------------------------------------------------------


def test_reloading_a_file_replaces_it_rather_than_doubling_it(engine, tmp_path):
    path = _write(tmp_path, "p.txt", PATTERSON)
    load_assessment(engine, path)
    load_assessment(engine, path)

    with engine.connect() as conn:
        docs = conn.execute(select(chna_documents.c.id)).all()
        needs = conn.execute(select(chna_needs.c.id)).all()

    assert len(docs) == 1
    assert len(needs) == 11


def test_reloading_leaves_no_orphaned_needs(engine, tmp_path):
    """SQLite does not enforce cascade unless foreign keys are switched on."""

    path = _write(tmp_path, "p.txt", PATTERSON)
    load_assessment(engine, path)
    load_assessment(engine, path)

    with engine.connect() as conn:
        doc_ids = {r.id for r in conn.execute(select(chna_documents.c.id))}
        need_docs = {r.document_id for r in conn.execute(select(chna_needs.c.document_id))}

    assert need_docs <= doc_ids


def test_one_document_published_under_two_names_loads_as_two_rows(engine, tmp_path):
    """Mercy files one system CHNA and posts it under each facility's name.

    Collapsing them loses a hospital; the source file is the key, not the text.
    """

    load_assessment(engine, _write(tmp_path, "MercyMoundridge.txt", CHEYENNE))
    load_assessment(engine, _write(tmp_path, "MercySEK.txt", CHEYENNE))

    with engine.connect() as conn:
        rows = conn.execute(select(chna_documents.c.source_file)).all()

    assert len(rows) == 2


# --- folders and failures ---------------------------------------------------


def test_a_folder_loads_every_document(engine, tmp_path):
    _write(tmp_path, "p.txt", PATTERSON)
    _write(tmp_path, "c.txt", CHEYENNE)

    summary = load_path(engine, str(tmp_path))

    assert summary.files == 2
    assert summary.loaded == 2


def test_the_summary_counts_what_could_not_be_attributed(engine, tmp_path):
    _hospital(engine, "170027", "PATTERSON HEALTH CENTER")
    _write(tmp_path, "p.txt", PATTERSON)
    _write(tmp_path, "c.txt", CHEYENNE)

    summary = load_path(engine, str(tmp_path))

    assert summary.attributed == 1
    assert [d.source_file for d in summary.unattributed] == ["c.txt"]


def test_an_unreadable_file_does_not_end_the_batch(engine, tmp_path):
    _write(tmp_path, "p.txt", PATTERSON)
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf at all")

    summary = load_path(engine, str(tmp_path))

    assert summary.loaded == 1
    assert summary.failed == 1
    assert summary.problems[0].source_file == "broken.pdf"


def test_files_that_are_not_documents_are_ignored(engine, tmp_path):
    _write(tmp_path, "p.txt", PATTERSON)
    _write(tmp_path, "notes.csv", "a,b,c")

    assert load_path(engine, str(tmp_path)).files == 1


# --- built for fifty states, not one ----------------------------------------


def test_a_system_document_can_cover_hospitals_it_never_names(engine, tmp_path):
    """Mercy files one assessment for a whole region. Without this the other
    facilities stay in the gap report while the document sits on the disk."""

    from hospitals.chna_load import cover_hospitals
    from hospitals.db import chna_document_hospitals

    _hospital(engine, "170027", "PATTERSON HEALTH CENTER")
    _hospital(engine, "170055", "MERCY MOUNDRIDGE")
    _hospital(engine, "170066", "MERCY COLUMBUS")
    load_assessment(engine, _write(tmp_path, "system.txt", PATTERSON))

    added = cover_hospitals(engine, "system.txt", ["170055", "170066"])

    with engine.connect() as conn:
        covered = {r.ccn for r in conn.execute(select(chna_document_hospitals.c.ccn))}

    assert added == 2
    assert covered == {"170027", "170055", "170066"}


def test_the_hospital_it_is_filed_under_is_covered_automatically(engine, tmp_path):
    from hospitals.db import chna_document_hospitals

    _hospital(engine, "170027", "PATTERSON HEALTH CENTER")
    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    with engine.connect() as conn:
        rows = conn.execute(select(chna_document_hospitals.c.ccn)).all()

    assert [r.ccn for r in rows] == ["170027"]


def test_covering_a_ccn_no_hospital_has_is_refused(engine, tmp_path):
    """An unjoinable row here is invisible: the hospital just keeps looking
    uncovered and nothing says why."""

    from hospitals.chna_load import cover_hospitals

    _hospital(engine, "170027", "PATTERSON HEALTH CENTER")
    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    assert cover_hospitals(engine, "p.txt", ["999999"]) == 0


def test_covering_the_same_hospital_twice_is_idempotent(engine, tmp_path):
    from hospitals.chna_load import cover_hospitals

    _hospital(engine, "170027", "PATTERSON HEALTH CENTER")
    _hospital(engine, "170055", "MERCY MOUNDRIDGE")
    load_assessment(engine, _write(tmp_path, "p.txt", PATTERSON))

    assert cover_hospitals(engine, "p.txt", ["170055"]) == 1
    assert cover_hospitals(engine, "p.txt", ["170055"]) == 0


def test_covering_an_unknown_document_says_so(engine):
    from hospitals.chna_load import cover_hospitals

    with pytest.raises(LookupError, match="no CHNA document"):
        cover_hospitals(engine, "never-loaded.pdf", ["170027"])


def test_reloading_a_document_clears_its_coverage(engine, tmp_path):
    """Otherwise a reload leaves rows pointing at a document id that is gone."""

    from hospitals.chna_load import cover_hospitals
    from hospitals.db import chna_document_hospitals

    _hospital(engine, "170027", "PATTERSON HEALTH CENTER")
    _hospital(engine, "170055", "MERCY MOUNDRIDGE")
    path = _write(tmp_path, "p.txt", PATTERSON)
    load_assessment(engine, path)
    cover_hospitals(engine, "p.txt", ["170055"])
    load_assessment(engine, path)

    with engine.connect() as conn:
        docs = {r.id for r in conn.execute(select(chna_documents.c.id))}
        links = {r.document_id for r in conn.execute(
            select(chna_document_hospitals.c.document_id))}

    assert links <= docs


# --- surveying a new state --------------------------------------------------


def test_a_survey_counts_the_formats_without_loading_anything(engine, tmp_path):
    from hospitals.chna_load import survey_path

    _write(tmp_path, "p.txt", PATTERSON)
    _write(tmp_path, "c.txt", CHEYENNE)
    _write(tmp_path, "other.txt", "A report prepared by Tripp Umbach for the county.")

    survey = survey_path(str(tmp_path))

    assert survey.files == 3
    assert survey.by_template == {"vvv": 2}
    assert survey.unidentified == ["other.txt"]


def test_an_unrecognised_document_reports_who_wrote_it(engine, tmp_path):
    """The next template entry should be a fact, not a guess."""

    from hospitals.chna_load import survey_path

    _write(tmp_path, "x.txt", "This assessment was prepared by Verite Healthcare "
                              "Consulting in partnership with the health department.")

    assert survey_path(str(tmp_path)).by_hint == {"Verite Healthcare Consulting": 1}


def test_a_document_naming_nobody_is_still_counted(engine, tmp_path):
    from hospitals.chna_load import survey_path

    _write(tmp_path, "x.txt", "Some report with no attribution line at all.")
    survey = survey_path(str(tmp_path))

    assert survey.unidentified == ["x.txt"]
    assert survey.by_hint == {}


def test_the_survey_separates_scans_from_junk(engine, tmp_path):
    from hospitals.chna_load import survey_path

    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    survey = survey_path(str(tmp_path))

    assert survey.unreadable == ["broken.pdf"]
    assert survey.scanned == []
