"""Reading an implementation strategy.

The fixtures are real rows from Patterson Health Center's 2024 plan (Harper
County) and The University of Kansas Health System — Olathe's FY2025–27
improvement plan, including the column wreckage that PDF extraction leaves
behind.
"""

from hospitals.strategy import parse_strategy

PATTERSON = """\
CHNA Health Areas of Need T "Specific Actions" to Address Community Health Need or "Reasons Why
Hospital Will Not" Identified "Lead" Identified Partners Timeframe  (Hours) $$$
1 Substance Abuse (Drugs & Alcohol) a
Support Mirror Substance abuse counseling for community members.
Mirror and PHC
EMS, Health Dept., Law
Ongoing 4 $320
This health need is a community Social determinant, thus not
part Hospital's Mission or Critical operations.  Will partner with
others as appropriate.
 c Continue clinic education on pain management. PHC Mirror, Horizons, and local
AA/recovery groups, KHA Ongoing 6 $480
2
Mental Health Services (Diagnosis,
Placement, Aftercare, Access to
Providers)
a
Explore the development of a formalized county-wide health coalition.
d Explore the option of providing education to all medical staff.
PHC Horizons 2025 $1,700
3 EMS (Staffing, Coverage, Funding) a Continue to support High School EMS Program to expose
students to this career choice.
f Explore the option of having the mobile clinic at the home football
games to provide coverage for minor injuries, etc. PHC, USD 361 2025 4 $300
Overall Total Contributions 256 $23,250
"""

OLATHE = """\
Priority #2: Address the rise in crisis behavioral healthcare.
NEED: In Johnson County, the average per capita income is $56,364 while 5.4% of the
population is in poverty.
Partner with the City of Olathe to support the behavioral health co-responder
program. X X X
Contribute $50,000 annually to subsidize the salary of the behavioral health
specialist employed through the Olathe Police Department.
Please note: This health need is a community social determinant, thus not part
hospital's mission or critical operations. Will partner with others as appropriate.
"""


# --- money and hours --------------------------------------------------------


def test_a_tabulated_row_yields_its_timeframe_hours_and_dollars():
    only = parse_strategy("a Do the thing. PHC Partners Ongoing 4 $320").commitments[0]

    assert only.timeframe == "Ongoing"
    assert only.hours == 4
    assert only.dollars == 320
    assert only.tabulated


def test_a_row_with_no_hours_still_yields_its_money():
    only = parse_strategy("d Explore staff education. PHC Horizons 2025 $1,700").commitments[0]

    assert only.hours is None
    assert only.timeframe == "2025"
    assert only.dollars == 1700


def test_thousands_separators_survive():
    assert parse_strategy("x Fund it. Ongoing 186 $7,400").commitments[0].dollars == 7400


def test_the_row_is_kept_verbatim_because_the_columns_are_gone():
    """Lead and partners are in the same string as the action, and the
    flattened text does not say where one ends."""

    text = "AA/recovery groups, KHA Ongoing 6 $480"
    assert parse_strategy(text).commitments[0].text == text


# --- a dollar figure is not always a promise --------------------------------


def test_a_statistic_about_the_county_is_not_a_commitment():
    """Olathe's plan states a $56,364 per capita income three pages from the
    $50,000 it actually commits. Adding them would be worse than reading
    neither."""

    money = [c.dollars for c in parse_strategy(OLATHE).commitments]

    assert 50000 in money
    assert 56364 not in money


def test_a_prose_promise_is_kept():
    only = parse_strategy(
        "Contribute $50,000 annually to subsidize the salary of the specialist."
    ).commitments[0]

    assert only.dollars == 50000
    assert not only.tabulated


def test_a_dollar_figure_with_no_verb_at_all_is_ignored():
    assert parse_strategy("The median household income is $48,200.").commitments == []


def test_a_descriptive_cue_beats_a_committing_one():
    """'salary of the' appears in both a promise and a description; the
    description wins when the sentence is plainly about the county."""

    text = "The average per capita income supports a salary of the kind described."
    assert parse_strategy(text).commitments == []


# --- the totals line --------------------------------------------------------


def test_the_plans_own_total_is_read():
    strategy = parse_strategy(PATTERSON)

    assert strategy.stated_total_dollars == 23250
    assert strategy.stated_total_hours == 256


def test_the_total_row_is_not_itself_a_commitment():
    money = [c.dollars for c in parse_strategy(PATTERSON).commitments]
    assert 23250 not in money


def test_reconciliation_reports_a_disagreement_rather_than_hiding_it():
    strategy = parse_strategy(
        "a Do a thing. Ongoing 2 $100\nOverall Total Contributions 9 $999"
    )

    assert strategy.committed_dollars == 100
    assert strategy.stated_total_dollars == 999
    assert strategy.reconciles is False


def test_a_plan_stating_no_total_cannot_be_reconciled():
    """None, not False -- there is nothing to disagree with."""

    assert parse_strategy("a Do a thing. Ongoing 2 $100").reconciles is None


# --- need headings ----------------------------------------------------------


def test_every_need_heading_is_found_in_all_three_shapes():
    needs = parse_strategy(PATTERSON).needs

    assert [rank for rank, _ in needs] == [1, 2, 3]


def test_a_heading_with_the_first_tactic_run_onto_it_is_still_a_heading():
    needs = parse_strategy(
        "1 EMS (Staffing, Coverage, Funding) a Continue to support the program."
    ).needs

    assert needs == [(1, "EMS (Staffing, Coverage, Funding)")]


def test_needs_must_arrive_in_sequence():
    """A plan numbers its needs 1..N. A heading out of sequence is a page
    number, a year, or a table row that happens to start with a digit."""

    needs = parse_strategy(
        "1 Substance Abuse a Do a thing.\n"
        "7 Some Other Heading a Do another thing.\n"
        "2 Mental Health a Do a third thing."
    ).needs

    assert [rank for rank, _ in needs] == [1, 2]


def test_a_slide_decks_page_numbers_are_not_needs():
    """A 21-page deck without the tabular plan format yielded twenty "needs"
    whose labels were whatever sentence followed each page number."""

    deck = "\n".join(f"{n}\nPriority text on page {n}." for n in range(1, 12))
    assert parse_strategy(deck).needs == []


def test_a_label_wrapped_after_a_lone_number_is_collected():
    strategy = parse_strategy(PATTERSON)
    label = dict(strategy.needs)[2]

    assert label.startswith("Mental Health Services")
    assert "Aftercare" in label


def test_a_commitment_is_filed_under_the_need_above_it():
    """A tactic under the wrong need makes the hospital appear to have promised
    something about a problem it was addressing elsewhere."""

    by_need = {}
    for c in parse_strategy(PATTERSON).commitments:
        by_need.setdefault(c.need_rank, []).append(c.dollars)

    assert sorted(by_need[1]) == [320, 480]
    assert by_need[2] == [1700]
    assert by_need[3] == [300]


def test_a_number_too_large_to_be_a_need_is_not_a_heading():
    assert parse_strategy("2024 Annual Report Summary").needs == []


# --- declines ---------------------------------------------------------------


def test_the_boilerplate_declines_are_counted():
    assert parse_strategy(PATTERSON).declines == 1
    assert parse_strategy(OLATHE).declines == 1


def test_a_plan_with_no_boilerplate_declines_reports_none():
    assert parse_strategy("We will address everything we found.").declines == 0


# --- whole documents --------------------------------------------------------


def test_an_empty_document_does_not_raise():
    strategy = parse_strategy("")

    assert strategy.commitments == []
    assert strategy.needs == []
    assert strategy.committed_dollars == 0


def test_free_prose_with_no_money_yields_no_commitments():
    """Clay County's strategy names no figures at all. Nothing is better than
    something invented."""

    prose = ("Objective 1: Enhance prevention and treatment resources. "
             "CCMC will collaborate with the health department to increase "
             "the availability of outpatient services.")
    assert parse_strategy(prose).commitments == []


def test_fiscal_year_columns_are_captured_where_present():
    only = parse_strategy("Launch training. FY 2025 FY 2026 Ongoing 2 $500").commitments[0]

    assert only.fiscal_years == ["2025", "2026"]
