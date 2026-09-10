import datetime as dt

from sqlalchemy import insert, select

from hospitals.db import charge_sources, hospitals, make_engine, metadata, standard_charges
from hospitals.merge_vaults import apply_merge, plan_merge


def _make_vault(path: str, *, with_hospitals: bool, sources: list[dict]):
    """Build a real sqlite file with the full schema and given charge_sources
    (each with its own handful of standard_charges rows)."""
    engine = make_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    with engine.begin() as conn:
        if with_hospitals:
            conn.execute(
                insert(hospitals),
                [{
                    "ccn": "171376", "name": "Girard Medical Center",
                    "provider_category_code": "01", "provider_subtype_code": "09",
                    "provider_subtype": "Critical Access Hospital", "state": "KS",
                    "is_active": True,
                }],
            )
        for src in sources:
            row = {
                "source_file": src["source_file"],
                "hospital_name": src["hospital_name"],
                "license_state": src.get("license_state", "MO"),
                "charge_count": src.get("charge_count", 2),
                "ingested_at": dt.datetime(2026, 1, 1),
            }
            result = conn.execute(insert(charge_sources), row)
            source_id = result.inserted_primary_key[0]
            conn.execute(
                insert(standard_charges),
                [
                    {"source_id": source_id, "description": f"item {i}", "gross_charge": 100 + i}
                    for i in range(src.get("charge_count", 2))
                ],
            )
    engine.dispose()


def test_plan_merge_classifies_new_vs_duplicate(tmp_path):
    target = str(tmp_path / "target.sqlite")
    source = str(tmp_path / "probe.sqlite")

    _make_vault(target, with_hospitals=True, sources=[
        {"source_file": "already_here.csv", "hospital_name": "Girard Medical Center", "charge_count": 3},
    ])
    _make_vault(source, with_hospitals=False, sources=[
        {"source_file": "already_here.csv", "hospital_name": "Girard Medical Center", "charge_count": 3},
        {"source_file": "new_hospital.csv", "hospital_name": "Some Missouri Hospital", "charge_count": 5},
    ])

    summary = plan_merge(target, source)
    assert {d.source_file for d in summary.to_add} == {"new_hospital.csv"}
    assert {d.source_file for d in summary.duplicates} == {"already_here.csv"}
    assert summary.applied is False


def test_apply_merge_copies_only_new_files_and_backs_up(tmp_path):
    target = str(tmp_path / "target.sqlite")
    source = str(tmp_path / "probe.sqlite")

    _make_vault(target, with_hospitals=True, sources=[
        {"source_file": "already_here.csv", "hospital_name": "Girard Medical Center", "charge_count": 3},
    ])
    _make_vault(source, with_hospitals=False, sources=[
        {"source_file": "already_here.csv", "hospital_name": "Girard Medical Center", "charge_count": 3},
        {"source_file": "new_hospital.csv", "hospital_name": "Some Missouri Hospital", "charge_count": 5},
    ])

    summary = apply_merge(target, source, backup_dir=str(tmp_path / "backups"))

    assert summary.applied is True
    assert summary.charge_rows_added == 5
    assert summary.backup_path and summary.backup_path.endswith(".bak")
    import os
    assert os.path.exists(summary.backup_path)

    engine = make_engine(f"sqlite:///{target}")
    with engine.connect() as conn:
        files = {r.source_file for r in conn.execute(select(charge_sources.c.source_file))}
        assert files == {"already_here.csv", "new_hospital.csv"}

        # The pre-existing file's rows were untouched (still 3), not duplicated.
        existing_id = conn.execute(
            select(charge_sources.c.id).where(charge_sources.c.source_file == "already_here.csv")
        ).scalar_one()
        existing_count = conn.execute(
            select(standard_charges.c.id).where(standard_charges.c.source_id == existing_id)
        ).all()
        assert len(existing_count) == 3

        # The new file's rows landed, pointing at the new (remapped) source id.
        new_id = conn.execute(
            select(charge_sources.c.id).where(charge_sources.c.source_file == "new_hospital.csv")
        ).scalar_one()
        new_rows = conn.execute(
            select(standard_charges.c.description).where(standard_charges.c.source_id == new_id)
        ).all()
        assert len(new_rows) == 5

        # hospitals table is untouched by the merge (target stays authoritative).
        hosp_count = conn.execute(select(hospitals.c.ccn)).all()
        assert len(hosp_count) == 1
    engine.dispose()


def test_apply_merge_is_noop_when_nothing_new(tmp_path):
    target = str(tmp_path / "target.sqlite")
    source = str(tmp_path / "probe.sqlite")
    same = [{"source_file": "only.csv", "hospital_name": "X", "charge_count": 1}]
    _make_vault(target, with_hospitals=True, sources=same)
    _make_vault(source, with_hospitals=False, sources=same)

    summary = apply_merge(target, source, backup_dir=str(tmp_path / "backups"))
    assert summary.applied is True
    assert summary.charge_rows_added == 0
    # No backup is made when there is nothing to add — nothing would be at risk.
    assert summary.backup_path is None
