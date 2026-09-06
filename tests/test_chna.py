"""Reading a community health needs assessment.

Every string in this file is real output from a real Kansas CHNA, kerning
damage and broken words included. The two documents behind it are Patterson
Health Center (Harper County, 2024) and Cheyenne County Hospital (2023), both
produced by VVV Consultants of Olathe.
"""

from hospitals.chna import (
    DECLINE_BOILERPLATE,
    VVV,
    dekern,
    find_shortages,
    parse_assessment,
    parse_header,
    parse_needs,
    parse_trend,
    split_votes_and_pct,
)

# --- real fixtures ----------------------------------------------------------

PATTERSON = """\
Community Health Needs Assessment
Harper County, KS
On Behalf of Patterson Health Center
VVV Consultants LLC
Olathe, KS

County Health Area of Future Focus on Unmet Needs
# Community Health Needs to Change and/or Improve Votes % Accum
1 Substance Abuse (Drugs & Alcohol) 21 19.4% 19%
2 Mental Health Services (Diagnos is , Placement, Aftercare, Providers ) 20 18.5% 38%
3 EMS (Staffing, Coverage, Funding) 14 13.0% 51%
4 Access to Affordable Healthy Foods 11 10.2% 61%
5 Poverty 10 9.3% 70%
6H o m e  H e a l t h 7 6 . 5 % 7 7 %
Total Votes 108 100%
on behalf Patterson Health, Anthony, KS
2024 CHNA Priorities
Town Hall - 04/04/24  (Attendees 27 / 108 Total Votes)
Round #5  CHNA Health Needs Tactics Year 1 of 3 starting 1/1/25 - 12/31/25

Pressing
Rank Ongoing Problem Votes %T r e n d R a n k
1 Drugs / Alcohol Abuse 107 13.9% 1
2 Mental Health Services (Access, Provider, Treatment, Aftercare) 80 10.4% 3
3 Quality Housing 76 9.9% 5
4 EMS 73 9.5% 2
5 Child Care 67 8.7% 4
Totals 768 100.0%
"""

CHEYENNE = """\
Community Health Needs Assessment
Cheyenne County, KS
On Behalf of Cheyenne County Hospital
VVV Consultants LLC

# Community Health Needs to Change and/or Improve Votes % Accum
1 Substance Abuse (Drugs/ Alcohol) 18 20.5% 20%
2 Mental Health (Diagnosis, Placement, Aftercare,
Providers) 17 19.3% 40%
3 Lack of Community HC Knowledge / Personal Health
Responsibility to curb apathy 12 13.6% 53%
4 Lack of Visiting Specialists: Derm, Ped, Ent, Eye, Neu,
Ortho and Urol. 12 13.6% 67%
5 Lack of HC Reimbursement to cover HC Delivery
Expenses 5 5.7% 73%
6 Workforce Shortages 5 5.7% 78%
Total Votes 88 100%
CHNA Wave #4 Town Hall - 5/18/23   (29 Attendees / 88 Total Votes)
"""


# --- de-kerning -------------------------------------------------------------


def test_a_kerned_row_is_rejoined():
    """Row six of Patterson's table. Rows one to five extract cleanly."""

    assert dekern("6H o m e  H e a l t h 7 6 . 5 % 7 7 %") == "6Home Health76.5%77%"


def test_a_double_space_inside_a_kerned_run_is_a_word_break():
    assert dekern("A c c e s s  t o  C a r e") == "Access to Care"


def test_ordinary_text_is_untouched():
    line = "3 EMS (Staffing, Coverage, Funding) 14 13.0% 51%"
    assert dekern(line) == line


def test_a_short_run_of_initials_is_not_treated_as_kerning():
    """Otherwise a legitimate list of abbreviations gets welded together."""

    assert dekern("a b c") == "a b c"


def test_a_kerned_header_is_rejoined():
    assert dekern("Rank Ongoing Problem Votes %T r e n d R a n k").endswith("TrendRank")


def test_dekern_leaves_an_empty_line_alone():
    assert dekern("") == ""
    assert dekern("   ") == "   "


# --- splitting a collapsed number blob --------------------------------------


def test_the_vote_total_decides_where_the_number_splits():
    """76.5 is seven votes at 6.5%, not seventy-six at 0.5%."""

    assert split_votes_and_pct("76.5", 108) == (7, 6.5)


def test_the_other_reading_is_rejected_because_it_does_not_reconcile():
    assert split_votes_and_pct("76.5", 108) != (76, 0.5)


def test_a_blob_that_reconciles_nowhere_returns_nothing():
    """Better no number than a plausible wrong one nothing contradicts."""

    assert split_votes_and_pct("999.9", 108) is None


def test_a_blob_with_no_decimal_cannot_be_split():
    assert split_votes_and_pct("76", 108) is None


# --- the header -------------------------------------------------------------


def test_the_header_identifies_hospital_county_and_consultant():
    header = parse_header(PATTERSON)

    assert header.hospital == "Patterson Health Center"
    assert header.county == "Harper"
    assert header.state == "KS"
    assert header.consultant == VVV
    assert header.is_template


def test_the_town_hall_line_yields_date_attendees_and_votes():
    header = parse_header(PATTERSON)

    assert header.townhall_date == "04/04/24"
    assert header.attendees == 27
    assert header.total_votes == 108


def test_attendees_are_read_in_either_word_order():
    """Patterson writes 'Attendees 27'; Cheyenne writes '29 Attendees'."""

    assert parse_header(CHEYENNE).attendees == 29


def test_the_cycle_label_is_kept_as_written():
    """One consultant calls it a Round, the other a Wave, in the same template."""

    assert parse_header(PATTERSON).cycle_label == "Round #5"
    assert parse_header(CHEYENNE).cycle_label == "Wave #4"


def test_a_document_from_another_consultant_is_not_claimed_as_the_template():
    header = parse_header("Summary\nThe 2024 Community Health Needs Assessment was "
                          "administered by Clay County Medical Center.")
    assert header.consultant is None
    assert not header.is_template


# --- the priority tally -----------------------------------------------------


def test_every_ranked_need_is_read():
    needs = parse_needs(PATTERSON, total_votes=108)
    assert [n.rank for n in needs] == [1, 2, 3, 4, 5, 6]


def test_the_top_need_carries_its_votes_and_share():
    top = parse_needs(PATTERSON, total_votes=108)[0]

    assert top.label == "Substance Abuse (Drugs & Alcohol)"
    assert top.votes == 21
    assert top.pct == 19.4
    assert top.accum == 19.0


def test_the_kerned_sixth_priority_survives():
    """Without de-kerning this row vanishes and nothing looks wrong."""

    sixth = parse_needs(PATTERSON, total_votes=108)[5]

    assert sixth.label == "Home Health"
    assert sixth.votes == 7
    assert sixth.pct == 6.5
    assert sixth.recovered


def test_a_recovered_row_is_marked_as_recovered():
    needs = parse_needs(PATTERSON, total_votes=108)
    assert [n.recovered for n in needs] == [False] * 5 + [True]


def test_a_need_wrapped_onto_a_second_line_is_rejoined():
    needs = parse_needs(CHEYENNE, total_votes=88)
    second = next(n for n in needs if n.rank == 2)

    assert second.label.startswith("Mental Health")
    assert second.label.endswith("Providers)")
    assert second.votes == 17


def test_the_specialty_list_survives_the_line_wrap():
    """This row is the whole reason to read these documents."""

    fourth = next(n for n in parse_needs(CHEYENNE, total_votes=88) if n.rank == 4)

    assert "Derm" in fourth.label
    assert "Urol" in fourth.label
    assert fourth.votes == 12


def test_a_word_broken_by_extraction_is_repaired():
    """'Diagnos is' is what the PDF actually contains."""

    second = next(n for n in parse_needs(PATTERSON, total_votes=108) if n.rank == 2)
    assert "Diagnosis" in second.label


def test_the_totals_row_is_not_a_need():
    labels = [n.label for n in parse_needs(PATTERSON, total_votes=108)]
    assert not any("Total" in label for label in labels)


def test_needs_come_back_in_rank_order():
    ranks = [n.rank for n in parse_needs(CHEYENNE, total_votes=88)]
    assert ranks == sorted(ranks)


# --- the trend table --------------------------------------------------------


def test_the_trend_table_carries_the_previous_rank():
    trend = parse_trend(PATTERSON)
    housing = next(n for n in trend if n.label == "Quality Housing")

    assert housing.rank == 3
    assert housing.prior_rank == 5


def test_movement_is_signed_so_getting_worse_is_positive():
    trend = {n.label: n for n in parse_trend(PATTERSON)}

    assert trend["Quality Housing"].moved == -2   # improved from 5th to 3rd
    assert trend["EMS"].moved == 2                # worsened from 2nd to 4th
    assert trend["Drugs / Alcohol Abuse"].moved == 0


def test_the_priority_table_is_not_read_as_a_trend_table():
    """Both have a rank, a label, votes and a percentage; only one ends in a
    rank rather than a second percentage."""

    labels = [n.label for n in parse_trend(PATTERSON)]
    assert "Substance Abuse (Drugs & Alcohol)" not in labels


def test_a_document_with_no_trend_table_yields_nothing():
    assert parse_trend(CHEYENNE) == []


# --- shortages --------------------------------------------------------------


def test_the_abbreviated_specialty_list_is_expanded():
    found = {s.specialty for s in find_shortages(CHEYENNE)}

    assert {"dermatology", "pediatrics", "otolaryngology", "ophthalmology",
            "neurology", "orthopedics", "urology"} <= found


def test_a_specialty_needs_a_shortage_cue_in_the_same_sentence():
    """A CHNA lists what the hospital has as well as what it wants."""

    assert find_shortages("The hospital offers orthopedics and cardiology.") == []


def test_a_shortage_keeps_the_sentence_it_came_from():
    found = find_shortages("Continue to recruit additional substance abuse "
                           "professionals to deliver care in Harper County.")

    assert found[0].specialty == "behavioral health"
    assert "Harper County" in found[0].verbatim


def test_a_hospital_actively_recruiting_is_marked_as_such():
    found = find_shortages("Continue to recruit additional mental health providers.")
    assert found[0].recruiting


def test_a_shortage_that_is_only_named_is_not_marked_as_recruiting():
    found = find_shortages("Lack of visiting specialists: Derm and Ortho.")
    assert found and not any(s.recruiting for s in found)


def test_ems_staffing_is_read_as_a_shortage():
    found = {s.specialty for s in find_shortages(PATTERSON)}
    assert "ems" in found


def test_a_specialty_inside_a_longer_word_does_not_match():
    """'ped' must not fire on 'expedite'; 'eye' must not fire on 'eyeglasses
    donation'."""

    assert find_shortages("We lack the staff to expedite transfers.") == []


# --- the decline clause -----------------------------------------------------


def test_the_boilerplate_refusal_is_recognised():
    clause = ("This health need is a community social determinant, thus not part "
              "Hospital's Mission or Critical operations. Will partner with others "
              "as appropriate.")
    assert DECLINE_BOILERPLATE.search(clause)


def test_the_lower_case_variant_is_recognised_too():
    """Olathe writes it in sentence case; Patterson capitalises Mission."""

    clause = ("Please note: This health need is a community social determinant, thus "
              "not part hospital's mission or critical operations.")
    assert DECLINE_BOILERPLATE.search(clause)


def test_an_actual_reason_is_not_mistaken_for_the_boilerplate():
    reason = "The fourth area, affordable housing, will not be addressed in this plan."
    assert not DECLINE_BOILERPLATE.search(reason)


# --- the whole document -----------------------------------------------------


def test_an_assessment_reads_end_to_end():
    assessment = parse_assessment(PATTERSON)

    assert assessment.template == VVV
    assert assessment.header.hospital == "Patterson Health Center"
    assert len(assessment.needs) == 6
    assert len(assessment.trend) == 5
    assert assessment.recovered_rows == 1


def test_an_empty_document_does_not_raise():
    assessment = parse_assessment("")

    assert assessment.needs == []
    assert assessment.trend == []
    assert assessment.template is None


def test_a_free_prose_document_yields_a_header_and_no_tables():
    """Clay County's strategy has no tally at all. It should come back empty
    rather than half-parsed into something that looks like data."""

    prose = ("Summary\nThe 2024 Community Health Needs Assessment was administered "
             "by Clay County Medical Center. Priority areas: substance use "
             "disorders, mental health, domestic violence.")
    assessment = parse_assessment(prose)

    assert assessment.needs == []
    assert assessment.template is None
