"""Adversarial tests: dates, regimes, ISINs, classification, extraction false positives.
Each test asserts the behaviour the README / instructions promise; a failure is a defect."""
from datetime import date

import pandas as pd
import pytest

import common
import normalize_and_merge as m
import parse_nse_records as p


# ---------- dates ------------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("15 August 2017", "2017-08-15"),       # month name containing "st "
    ("August 1, 2017", "2017-08-01"),
    ("1st August 2017", "2017-08-01"),
    ("21st March 2020", "2020-03-21"),
    ("March 21st, 2020", "2020-03-21"),
    ("2017-03-04", "2017-03-04"),           # ISO must not be read day-first
    ("04-03-2017", "2017-03-04"),           # Indian dd-mm-yyyy
    ("4.3.2017", "2017-03-04"),
    ("24-Mar-17", "2017-03-24"),
    ("Mar 4, 2017", "2017-03-04"),
    ("23rd August, 2007", "2007-08-23"),
])
def test_to_iso_formats(text, want):
    assert p.to_iso(text) == want


def test_to_iso_rejects_impossible_dates():
    assert p.to_iso("31-09-2017") is None   # 31 September does not exist
    assert p.to_iso("30-02-2020") is None


def test_to_iso_excel_serial_number():
    # openpyxl usually gives datetimes, but CSV exports of the same sheet carry serials
    assert p.to_iso("42818") is None or p.to_iso("42818") == "2017-03-24"


# ---------- regimes / FY boundaries ------------------------------------------------------------
@pytest.mark.parametrize("d,want", [
    (date(2009, 6, 9), "SEBI_2003_Guidelines"), (date(2009, 6, 10), "SEBI_2009_Regs"),
    (date(2021, 6, 9), "SEBI_2009_Regs"), (date(2021, 6, 10), "SEBI_2021_Regs"),
    (date(2024, 9, 24), "SEBI_2021_Regs"), (date(2024, 9, 25), "SEBI_2024_Amendment"),
])
def test_regime_boundaries(d, want):
    assert m.regime_for(d) == want


def test_fy_boundaries():
    assert m.financial_year(date(2017, 3, 31)) == "FY2016-17"
    assert m.financial_year(date(2017, 4, 1)) == "FY2017-18"
    assert m.financial_year(date(2099, 4, 1)) == "FY2099-00"


def test_regime_uses_effective_date_not_order_date():
    # order under 2009 regs, effective after 2021 regs: README says regime is set by (effective) date
    r = m.normalize({"company_name": "X Ltd", "order_date": "2021-05-01", "effective_delisting_date": "2021-07-01"})
    assert r["regulatory_regime"] == "SEBI_2021_Regs"


# ---------- ISINs ------------------------------------------------------------------------------
def test_isin_validation_edges():
    assert common.isin_valid("INE002A01018")
    assert not common.isin_valid("ine002a01018")        # caller must upper-case
    assert not common.isin_valid("INE002A0101")          # 11 chars
    assert not common.isin_valid("INE002A010188")        # 13 chars
    assert not common.isin_valid("")
    assert not common.isin_valid(None)
    assert not common.isin_valid("INF209K01YN0")         # mutual fund ISIN (not equity)


def test_table_isin_with_spaces_or_lowercase_is_recovered():
    df = pd.DataFrame([["Company Name", "ISIN", "Date of Delisting"],
                       ["ABC Industries Ltd", "ine002a01018", "05-06-2017"],
                       ["XYZ Industries Ltd", "INE 009A01021", "05-06-2017"]])
    rows = p.rows_from_table(df, "OTHER")
    assert [r["isin"] for r in rows] == ["INE002A01018", "INE009A01021"]


def test_normalize_drops_invalid_isin():
    # a malformed ISIN coming from any extractor must not become the dedupe key / reach the master
    r = m.normalize({"company_name": "ABC Ltd", "isin": "INE002A01019", "effective_delisting_date": "2017-01-01"})
    assert r["isin"] is None


# ---------- classification ---------------------------------------------------------------------
def test_company_name_does_not_decide_the_type():
    df = pd.DataFrame([["Company Name", "Symbol", "Date of Delisting", "Reason"],
                       ["Amalgamated Electricity Co Ltd", "AMALELEC", "05-06-2017", "Compulsory"],
                       ["Liquidity Finance Ltd", "LIQFIN", "05-06-2017", ""]])
    rows = p.rows_from_table(df, "COMPULSORY")
    assert [r["delisting_type"] for r in rows] == ["COMPULSORY", "COMPULSORY"]


def test_voluntary_doc_mentioning_nclt_in_passing_is_not_ibc():
    text = ("Voluntary delisting of equity shares. Exit offer at the discovered price of Rs 120. "
            "Note: the acquirer is not under any NCLT proceedings.")
    assert p.classify(text) == "VOLUNTARY_EXIT_OFFER"


def test_ibc_substring_false_positive():
    assert p.classify("delisting of Vibcon Industries Ltd") != "IBC_NCLT"


# ---------- extraction false positives ---------------------------------------------------------
@pytest.mark.parametrize("line", [
    "ISIN: INE002A01018",
    "ISIN of the Company INE002A01018",
    "ISIN Code INE002A01018 Face Value Rs 10",
    "Scrip Code 500325 ISIN INE002A01018",
])
def test_isin_label_lines_are_not_companies(line):
    rows = p.rows_from_text(line, "OTHER")
    assert rows == [], rows


def test_last_traded_price_is_not_exit_price():
    df = pd.DataFrame([["Company Name", "Symbol", "Date of Delisting", "Last Traded Price"],
                       ["ABC Industries Ltd", "ABCIND", "05-06-2017", "3.45"]])
    [row] = p.rows_from_table(df, "COMPULSORY")
    assert row["exit_or_fair_value_inr"] is None


@pytest.mark.parametrize("cell,want", [("Rs. 125.50", 125.5), ("₹ 1,234.00", 1234.0), ("125.50 per share", 125.5),
                                       ("Rs.125/-", 125.0)])
def test_exit_price_parsing(cell, want):
    df = pd.DataFrame([["Company Name", "Symbol", "Date of Delisting", "Exit Price"],
                       ["ABC Industries Ltd", "ABCIND", "05-06-2017", cell]])
    [row] = p.rows_from_table(df, "VOLUNTARY_EXIT_OFFER")
    assert row["exit_or_fair_value_inr"] == want


def test_existing_test_order_title_uses_literal_backslash_n():
    """tests/test_parse.py::test_order_title_that_is_not_a_company_is_skipped passes "\\n" (two characters),
    so MATTER_RE never matches and the name_ok() guard it claims to cover is never reached."""
    s = "Before the Whole Time Member\\nIn the matter of Non- Obtaining of SCORES Authentication\\n"
    assert p.MATTER_RE.search(s[:4000]) is None        # demonstrates the test never reaches name_ok
    real = "Before the Whole Time Member\nIn the matter of Non- Obtaining of SCORES Authentication\n"
    assert p.rows_from_order(real) == []               # the real-newline variant: does the guard hold?


def test_relevance_by_harvested_from_page_alone():
    """Any document linked from a page whose URL says 'delist' is 'relevant', even an annual report."""
    from pathlib import Path
    ok, why = p.is_relevant(Path("x.pdf"), "https://www.nseindia.com/corporate/AnnualReport_2017.pdf",
                            "https://www.nseindia.com/regulations/delisting", {"title": "", "text_head": "Annual Report"})
    assert not ok, why


def test_proposed_regex_hits_ordinary_words():
    # "proposal" in a completed-delisting URL routes real delistings to the leads file
    assert not p.PROPOSED_RE.search("https://www.bseindia.com/delisting/approved-delisting-proposals-2017.pdf")
