"""Asking the store what a billing code costs.

Every command so far has been about getting data in and attributing it. This
is the first that asks the data a question, and the shape of the answer
matters: three prices that are not close to each other, reported as
percentiles because a chargemaster's tail makes an average nobody pays.
"""

import datetime as dt

import pytest
from sqlalchemy import insert

from hospitals.db import charge_sources, hospitals, init_db, make_engine, standard_charges
from hospitals.price import percentile, price_for_code

NOW = dt.datetime(2026, 8, 24)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'p.sqlite'}")
    init_db(eng)
    return eng


def _hospital(engine, ccn, name, state):
    with engine.begin() as conn:
        conn.execute(
            insert(hospitals),
            dict(ccn=ccn, name=name, state=state, is_active=True, ingested_at=NOW),
        )


def _source(engine, source_file, *, ccn=None, charges=()):
    """One charge file and its rows: (code, gross, cash, negotiated, payer)."""

    with engine.begin() as conn:
        result = conn.execute(
            insert(charge_sources),
            dict(
                source_file=source_file,
                ccn=ccn,
                charge_count=len(charges),
                ingested_at=NOW,
            ),
        )
        source_id = int(result.inserted_primary_key[0])
        if charges:
            conn.execute(
                insert(standard_charges),
                [
                    {
                        "source_id": source_id,
                        "code": code,
                        "description": "CBC WITH DIFFERENTIAL",
                        "gross_charge": gross,
                        "discounted_cash": cash,
                        "negotiated_dollar": negotiated,
                        "payer_name": payer,
                    }
                    for code, gross, cash, negotiated, payer in charges
                ],
            )


CBC = "85025"


# --- percentiles ----------------------------------------------------------


def test_the_median_of_an_odd_sample_is_the_middle_value():
    assert percentile([10, 20, 30], 50) == 20


def test_the_median_of_an_even_sample_interpolates():
    assert percentile([10, 20, 30, 40], 50) == 25


def test_quartiles_match_the_standard_method():
    # Type 7, as NumPy and R use by default.
    assert percentile([1, 2, 3, 4, 5], 25) == 2
    assert percentile([1, 2, 3, 4, 5], 75) == 4


def test_an_empty_sample_has_no_percentile():
    assert percentile([], 50) is None


def test_one_value_is_its_own_percentile():
    assert percentile([42], 25) == 42


# --- the three prices -----------------------------------------------------


def test_gross_cash_and_negotiated_are_reported_separately(engine):
    """They are not close to each other, so one number would mislead."""

    _hospital(engine, "170027", "PRATT REGIONAL MEDICAL CENTER", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[
        (CBC, 160, 112, 44, "Aetna"),
        (CBC, 160, 112, 38, "Blue Cross"),
    ])

    report = price_for_code(engine, CBC)

    assert report.gross.median == 160
    assert report.cash.median == 112
    assert report.negotiated.median == 41
    assert report.rows == 2
    assert report.hospital_count == 1


def test_the_common_description_is_reported(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[(CBC, 160, 112, 44, "Aetna")])

    assert price_for_code(engine, CBC).common_description == "CBC WITH DIFFERENTIAL"


def test_a_code_nobody_prices_reports_nothing(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[(CBC, 160, 112, 44, "Aetna")])

    report = price_for_code(engine, "99999")
    assert report.rows == 0
    assert report.negotiated.median is None


# --- what counts as a price -----------------------------------------------


def test_zero_and_missing_prices_are_not_prices(engine):
    """A placeholder zero would drag every median toward nothing."""

    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[
        (CBC, 160, 112, 44, "Aetna"),
        (CBC, 0, None, 0, "Placeholder"),
    ])

    report = price_for_code(engine, CBC)
    assert report.negotiated.count == 1
    assert report.negotiated.median == 44
    assert report.gross.count == 1


def test_rows_are_counted_even_when_their_prices_are_not(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[(CBC, 0, None, None, "X")])

    report = price_for_code(engine, CBC)
    assert report.rows == 1
    assert report.negotiated.count == 0


# --- scope ----------------------------------------------------------------


def test_unlinked_files_are_excluded_by_default(engine):
    """'Across N hospitals' should mean N identified hospitals."""

    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "linked.csv", ccn="170027", charges=[(CBC, 160, 112, 44, "Aetna")])
    _source(engine, "orphan.csv", charges=[(CBC, 999, 800, 500, "Aetna")])

    assert price_for_code(engine, CBC).negotiated.median == 44
    assert price_for_code(engine, CBC, linked_only=False).negotiated.count == 2


def test_a_state_filter_narrows_to_that_state(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _hospital(engine, "100080", "HCA FLORIDA JFK", "FL")
    _source(engine, "ks.csv", ccn="170027", charges=[(CBC, 160, 112, 44, "Aetna")])
    _source(engine, "fl.csv", ccn="100080", charges=[(CBC, 300, 200, 90, "Aetna")])

    assert price_for_code(engine, CBC, state="KS").negotiated.median == 44
    assert price_for_code(engine, CBC).hospital_count == 2


def test_a_payer_filter_narrows_to_that_payer(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[
        (CBC, 160, 112, 44, "Aetna Better Health"),
        (CBC, 160, 112, 88, "United Healthcare"),
    ])

    assert price_for_code(engine, CBC, payer="aetna").negotiated.median == 44


# --- by payer -------------------------------------------------------------


def test_each_payer_gets_its_own_median(engine):
    """The differentiating fact: what a specific insurer actually pays."""

    _hospital(engine, "170027", "PRATT", "KS")
    _hospital(engine, "100080", "JFK", "FL")
    _source(engine, "a.csv", ccn="170027", charges=[
        (CBC, 160, 112, 40, "Aetna"),
        (CBC, 160, 112, 90, "United"),
    ])
    _source(engine, "b.csv", ccn="100080", charges=[
        (CBC, 300, 200, 50, "Aetna"),
    ])

    payers = {p.payer: p for p in price_for_code(engine, CBC).top_payers}
    assert payers["Aetna"].median == 45
    assert payers["Aetna"].count == 2
    assert payers["United"].median == 90


def test_payers_are_ordered_by_how_much_they_appear(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[
        (CBC, 160, 112, 40, "Aetna"),
        (CBC, 160, 112, 41, "Aetna"),
        (CBC, 160, 112, 90, "United"),
    ])

    assert [p.payer for p in price_for_code(engine, CBC).top_payers] == ["Aetna", "United"]


def test_an_unnamed_payer_is_labelled_rather_than_dropped(engine):
    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[(CBC, 160, 112, 44, None)])

    assert price_for_code(engine, CBC).top_payers[0].payer == "(unnamed)"


# --- the spread is the point ----------------------------------------------


def test_the_quartiles_show_how_wide_the_range_is(engine):
    """One hospital at $400 is why this reports medians, not means."""

    _hospital(engine, "170027", "PRATT", "KS")
    _source(engine, "a.csv", ccn="170027", charges=[
        (CBC, 160, 112, price, "Payer") for price in (20, 30, 40, 50, 400)
    ])

    report = price_for_code(engine, CBC)
    assert report.negotiated.median == 40
    assert report.negotiated.p25 == 30
    assert report.negotiated.p75 == 50
    assert report.negotiated.high == 400
    assert report.negotiated.low == 20
