import datetime as dt

from hospitals import normalize
from hospitals.states import resolve_state, state_for_ssa_code


def test_normalize_ccn_pads_short_numeric():
    assert normalize.normalize_ccn("170") == "000170"
    assert normalize.normalize_ccn("170012") == "170012"
    assert normalize.normalize_ccn("  170012 ") == "170012"


def test_normalize_ccn_keeps_alphanumeric():
    assert normalize.normalize_ccn("17T012") == "17T012"
    assert normalize.normalize_ccn("") is None
    assert normalize.normalize_ccn(None) is None


def test_normalize_zip_drops_plus4():
    assert normalize.normalize_zip("67214-1234") == "67214"
    assert normalize.normalize_zip("67214") == "67214"
    assert normalize.normalize_zip("6721") == "06721"
    assert normalize.normalize_zip(None) is None


def test_normalize_phone_strips_formatting_and_country_code():
    assert normalize.normalize_phone("(316) 555-0100") == "3165550100"
    assert normalize.normalize_phone("1-785-555-0199") == "7855550199"
    assert normalize.normalize_phone("620 555 0144") == "6205550144"
    assert normalize.normalize_phone("n/a") is None


def test_parse_date_handles_pos_formats():
    assert normalize.parse_date("19850101") == dt.date(1985, 1, 1)
    assert normalize.parse_date("1985-01-01") == dt.date(1985, 1, 1)
    assert normalize.parse_date("01/01/1985") == dt.date(1985, 1, 1)
    assert normalize.parse_date("") is None


def test_clean_str_collapses_whitespace():
    assert normalize.clean_str("  A   B  ") == "A B"
    assert normalize.clean_str("   ") is None


def test_state_lookup_and_ssa_prefix():
    ks = resolve_state("KS")
    assert ks.name == "Kansas"
    assert ks.ssa_code == "17"
    assert resolve_state("maryland").usps == "MD"
    assert state_for_ssa_code("17").usps == "KS"
    assert state_for_ssa_code("21").usps == "MD"


def test_normalize_record_maps_and_decodes():
    raw = {
        "PRVDR_NUM": "170012",
        "PRVDR_CTGRY_CD": "01",
        "PRVDR_CTGRY_SBTYP_CD": "01",
        "FAC_NAME": "SUNFLOWER GENERAL HOSPITAL",
        "ST_ADR": "1234 MAIN ST",
        "CITY_NAME": "WICHITA",
        "STATE_CD": "KS",
        "ZIP_CD": "67214-1234",
        "TEL_NUM": "(316) 555-0100",
        "PGM_TRMNTN_CD": "00",
        "CRTFCTN_DT": "19850101",
        "GNRL_CNTL_TYPE_CD": "05",
        "CRTFD_BED_CNT": "220",
    }
    rec = normalize.normalize_record(raw)
    assert rec.ccn == "170012"
    assert rec.state == "KS"
    assert rec.zip5 == "67214"
    assert rec.phone == "3165550100"
    assert rec.provider_subtype == "Short-term (Acute Care)"
    assert rec.ownership_type == "Proprietary - Corporation"
    assert rec.certified_bed_count == 220
    assert rec.certification_date == dt.date(1985, 1, 1)
    assert rec.is_active is True
    assert rec.ssa_state_code == "17"


def test_is_hospital_and_is_active():
    hospital = {"PRVDR_CTGRY_CD": "01", "PGM_TRMNTN_CD": "00"}
    snf = {"PRVDR_CTGRY_CD": "03", "PGM_TRMNTN_CD": "00"}
    closed = {"PRVDR_CTGRY_CD": "01", "PGM_TRMNTN_CD": "08"}
    assert normalize.is_hospital(hospital) is True
    assert normalize.is_hospital(snf) is False
    assert normalize.is_active(hospital) is True
    assert normalize.is_active(closed) is False


def test_state_falls_back_to_ccn_when_column_missing():
    raw = {"PRVDR_NUM": "170012", "PRVDR_CTGRY_CD": "01", "PGM_TRMNTN_CD": "00"}
    rec = normalize.normalize_record(raw)
    assert rec.state == "KS"
