"""Decoding HCRIS worksheet/line/column cells into named financial metrics.

The crosswalk itself (hcris_crosswalk.py) is carried over verbatim from a
published, cited source and is not re-verified here -- these tests check
only that the matching/summing/materializing logic in hcris_metrics.py
applies that crosswalk correctly: single-line lookups, ranged sums, alpha
fields, disabled-by-default entries, and "no data means absent, not zero."
"""

from decimal import Decimal

from sqlalchemy import select

from hospitals.db import (
    hcris_alpha,
    hcris_metric_values,
    hcris_numeric,
    hcris_reports,
    init_db,
    make_engine,
)
from hospitals.hcris_metrics import compute_report_metrics, materialize_metrics


def _engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'v.sqlite'}")
    init_db(engine)
    return engine


def _seed_report(conn, *, rpt_rec_num: int, vintage_year: int, ccn: str) -> int:
    result = conn.execute(
        hcris_reports.insert().values(
            rpt_rec_num=rpt_rec_num,
            vintage_year=vintage_year,
            raw_line=f"{rpt_rec_num},4,{ccn},,2,01/01/2024,12/31/2024,06/01/2025",
            ccn=ccn,
        )
    )
    return result.inserted_primary_key[0]


def test_compute_report_metrics_reads_worksheet_g_balance_sheet_lines(tmp_path):
    """Worksheet G (the balance sheet, wksht_cd G000000) is a distinct
    worksheet from G-3 (G300000) with its own independent line numbering --
    line 1 on G is cash on hand, not net patient revenue."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        report_id = _seed_report(conn, rpt_rec_num=5010, vintage_year=2024, ccn="170040")
        conn.execute(
            hcris_numeric.insert(),
            [
                {"report_id": report_id, "wksht_cd": "G000000", "clmn_num": "00100",
                 "line_num": "00100", "value": Decimal("250000")},  # cash_on_hand
                {"report_id": report_id, "wksht_cd": "G000000", "clmn_num": "00100",
                 "line_num": "01100", "value": Decimal("500000")},  # total_current_assets
                {"report_id": report_id, "wksht_cd": "G000000", "clmn_num": "00100",
                 "line_num": "03600", "value": Decimal("2000000")},  # total_assets
                {"report_id": report_id, "wksht_cd": "G300000", "clmn_num": "00100",
                 "line_num": "02900", "value": Decimal("150000")},  # net_income
            ],
        )

    metrics = compute_report_metrics(engine, report_id)

    assert metrics["cash_on_hand"] == Decimal("250000.0000")
    assert metrics["total_current_assets"] == Decimal("500000.0000")
    assert metrics["total_assets"] == Decimal("2000000.0000")
    assert metrics["net_income"] == Decimal("150000.0000")


def test_compute_report_metrics_reads_worksheet_a_a7_and_s3_part_ii(tmp_path):
    """A700003 (A-7 Part III) and S300002 (S-3 Part II) weren't named by
    either published source -- they were confirmed against this project's
    own stored data, so the shape here mirrors the real worksheet exactly:
    A-7 Part III's figures all sit on line 3, distinguished only by column."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        report_id = _seed_report(conn, rpt_rec_num=5020, vintage_year=2024, ccn="170050")
        conn.execute(
            hcris_numeric.insert(),
            [
                {"report_id": report_id, "wksht_cd": "A000000", "clmn_num": "00100",
                 "line_num": "20000", "value": Decimal("400000")},  # salary_expense
                {"report_id": report_id, "wksht_cd": "A000000", "clmn_num": "00200",
                 "line_num": "00400", "value": Decimal("80000")},  # fringe_benefits
                {"report_id": report_id, "wksht_cd": "A700003", "clmn_num": "00900",
                 "line_num": "00300", "value": Decimal("45000")},  # depreciation_expense
                {"report_id": report_id, "wksht_cd": "A700003", "clmn_num": "01000",
                 "line_num": "00300", "value": Decimal("10000")},  # lease_cost
                {"report_id": report_id, "wksht_cd": "A700003", "clmn_num": "01100",
                 "line_num": "00300", "value": Decimal("25000")},  # interest_expense
                {"report_id": report_id, "wksht_cd": "S300002", "clmn_num": "00400",
                 "line_num": "00701", "value": Decimal("5000")},  # contract_labor_addon
                {"report_id": report_id, "wksht_cd": "S300002", "clmn_num": "00400",
                 "line_num": "01200", "value": Decimal("15000")},  # contract_labor_main
            ],
        )

    metrics = compute_report_metrics(engine, report_id)

    assert metrics["salary_expense"] == Decimal("400000.0000")
    assert metrics["fringe_benefits"] == Decimal("80000.0000")
    assert metrics["depreciation_expense"] == Decimal("45000.0000")
    assert metrics["lease_cost"] == Decimal("10000.0000")
    assert metrics["interest_expense"] == Decimal("25000.0000")
    assert metrics["contract_labor_addon"] == Decimal("5000.0000")
    assert metrics["contract_labor_main"] == Decimal("15000.0000")


def test_compute_report_metrics_reads_single_line_and_summed_range(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        report_id = _seed_report(conn, rpt_rec_num=5001, vintage_year=2024, ccn="170027")
        conn.execute(
            hcris_numeric.insert(),
            [
                # netpatrev: single line (G300000, col 00100, line 00300)
                {"report_id": report_id, "wksht_cd": "G300000", "clmn_num": "00100",
                 "line_num": "00300", "value": Decimal("1000000.00")},
                # opexp: single line (G300000, col 00100, line 00400)
                {"report_id": report_id, "wksht_cd": "G300000", "clmn_num": "00100",
                 "line_num": "00400", "value": Decimal("900000.00")},
                # icu_beds: ranged sum (S300001, col 00200, lines 00800-00899)
                {"report_id": report_id, "wksht_cd": "S300001", "clmn_num": "00200",
                 "line_num": "00800", "value": Decimal("5")},
                {"report_id": report_id, "wksht_cd": "S300001", "clmn_num": "00200",
                 "line_num": "00850", "value": Decimal("3")},
                # out of range -- must not be swept into icu_beds
                {"report_id": report_id, "wksht_cd": "S300001", "clmn_num": "00200",
                 "line_num": "00900", "value": Decimal("99")},
            ],
        )
        conn.execute(
            hcris_alpha.insert(),
            [
                {"report_id": report_id, "wksht_cd": "S200001", "clmn_num": "00100",
                 "line_num": "00300", "value": "Test Hospital"},
            ],
        )

    metrics = compute_report_metrics(engine, report_id)

    assert metrics["netpatrev"] == Decimal("1000000.0000")
    assert metrics["opexp"] == Decimal("900000.0000")
    assert metrics["icu_beds"] == Decimal("8")  # 5 + 3, not 99's line
    assert metrics["hospital_name"] == "Test Hospital"


def test_a_metric_with_no_matching_data_is_absent_not_zero(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        report_id = _seed_report(conn, rpt_rec_num=5002, vintage_year=2024, ccn="170028")
        # No hcris_numeric/hcris_alpha rows at all for this report.

    metrics = compute_report_metrics(engine, report_id)
    assert metrics == {}
    assert "netpatrev" not in metrics


def test_disabled_crosswalk_entries_are_excluded_by_default(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        report_id = _seed_report(conn, rpt_rec_num=5003, vintage_year=2024, ccn="170029")
        conn.execute(
            hcris_numeric.insert(),
            [
                # costinitchcare is enabled=False in the crosswalk.
                {"report_id": report_id, "wksht_cd": "S100000", "clmn_num": "00300",
                 "line_num": "02100", "value": Decimal("12345")},
            ],
        )

    assert compute_report_metrics(engine, report_id) == {}
    enabled = compute_report_metrics(engine, report_id, include_disabled=True)
    assert enabled["costinitchcare"] == Decimal("12345.0000")


def test_materialize_metrics_writes_and_replaces_a_vintage(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        report_id = _seed_report(conn, rpt_rec_num=5004, vintage_year=2024, ccn="170030")
        conn.execute(
            hcris_numeric.insert(),
            [
                {"report_id": report_id, "wksht_cd": "G300000", "clmn_num": "00100",
                 "line_num": "00300", "value": Decimal("500000")},
            ],
        )

    summary = materialize_metrics(engine, vintage_year=2024)
    assert summary.reports_processed == 1
    assert summary.values_written == 1

    with engine.connect() as conn:
        rows = conn.execute(select(hcris_metric_values)).all()
    assert len(rows) == 1
    assert rows[0].metric == "netpatrev"
    assert rows[0].value_numeric == Decimal("500000.0000")
    assert rows[0].value_text is None

    # Re-running must replace, not duplicate.
    summary2 = materialize_metrics(engine, vintage_year=2024)
    assert summary2.values_written == 1
    with engine.connect() as conn:
        assert len(conn.execute(select(hcris_metric_values)).all()) == 1


def test_materialize_metrics_only_touches_its_own_vintage(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        r2023 = _seed_report(conn, rpt_rec_num=6001, vintage_year=2023, ccn="170031")
        r2024 = _seed_report(conn, rpt_rec_num=6002, vintage_year=2024, ccn="170031")
        conn.execute(
            hcris_numeric.insert(),
            [
                {"report_id": r2023, "wksht_cd": "G300000", "clmn_num": "00100",
                 "line_num": "00300", "value": Decimal("111")},
                {"report_id": r2024, "wksht_cd": "G300000", "clmn_num": "00100",
                 "line_num": "00300", "value": Decimal("222")},
            ],
        )

    materialize_metrics(engine, vintage_year=2023)
    materialize_metrics(engine, vintage_year=2024)

    with engine.connect() as conn:
        values = {
            r.report_id: r.value_numeric
            for r in conn.execute(select(hcris_metric_values))
        }
    assert values[r2023] == Decimal("111.0000")
    assert values[r2024] == Decimal("222.0000")

    # Re-materializing 2023 alone must not touch 2024's already-written rows.
    materialize_metrics(engine, vintage_year=2023)
    with engine.connect() as conn:
        assert len(conn.execute(select(hcris_metric_values)).all()) == 2
