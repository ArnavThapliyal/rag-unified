from pathlib import Path

import pandas as pd

import parse_nse_records as p

ORDER_TEXT = """Before the Delisting Committee ("Committee")
of
National Stock Exchange of India Limited
In the matter of the Company: SecUR Credentials Limited (SECURCRED)
1. Background
... granted final opportunity ... failing which the Company shall be delisted from the Exchange, without any
further opportunity being granted.
... the Committee directed that the Equity shares of SecUR Credentials Limited (SECURCRED) be compulsorily
delisted from the Exchange in terms of Regulation 32(1) of SEBI (Delisting of Equity Shares) Regulations, 2021.
Sd/-
Date: October 9, 2026
"""


def test_to_iso_is_day_first_and_bounded():
    assert p.to_iso("05-06-2017") == "2017-06-05"
    assert p.to_iso("October 9, 2026") == "2026-10-09"
    assert p.to_iso("12/09/2020") == "2020-09-12"
    assert p.to_iso("not a date") is None
    assert p.to_iso("01-01-1890") is None


def test_classify_keywords():
    assert p.classify("compulsory delisting order") == "COMPULSORY"
    assert p.classify("NCLT approved resolution plan") == "IBC_NCLT"
    assert p.classify("scheme of amalgamation") == "AMALGAMATION_MERGER"
    assert p.classify("exit offer at discovered price") == "VOLUNTARY_EXIT_OFFER"
    assert p.classify("voluntary delisting") == "VOLUNTARY_NO_EXIT"
    assert p.classify("something else") == "OTHER"


def test_order_extractor_prefers_compulsory_decision():
    [row] = p.rows_from_order(ORDER_TEXT)
    assert row["company_name"] == "SecUR Credentials Limited"
    assert row["nse_symbol"] == "SECURCRED"
    assert row["order_date"] == "2026-10-09"
    assert row["delisting_type"] == "COMPULSORY"


def test_order_extractor_ignores_other_documents():
    assert p.rows_from_order("Quarterly report on listed companies") == []


def test_table_rows_header_detection_and_junk_names():
    df = pd.DataFrame([
        ["Sr", "Company Name", "Symbol", "ISIN", "Date of Delisting", "Reason"],
        [1, "ABC Industries Ltd", "abcind", "INE002A01018", "05-06-2017", "Compulsory delisting"],
        [2, "XYZ Ltd", "XYZ", "INE009A01021", "12/09/2020", "NCLT resolution plan"],
        [3, "NaT", None, None, None, None],
    ])
    rows = p.rows_from_table(df, "OTHER")
    assert [r["company_name"] for r in rows] == ["ABC Industries Ltd", "XYZ Ltd"]
    assert rows[0]["nse_symbol"] == "ABCIND"
    assert rows[0]["effective_delisting_date"] == "2017-06-05"
    assert rows[0]["delisting_type"] == "COMPULSORY"
    assert rows[1]["delisting_type"] == "IBC_NCLT"


def test_text_rows_take_symbol_after_isin():
    [row] = p.rows_from_text("1. ABC Industries Limited INE002A01018 ABCIND 08 June 2017", "COMPULSORY")
    assert row["isin"] == "INE002A01018"
    assert row["nse_symbol"] == "ABCIND"
    assert row["effective_delisting_date"] == "2017-06-08"


def test_relevance_gate():
    pdf, html = Path("x.pdf"), Path("x.html")
    no_text = {"title": "", "text_head": ""}
    assert p.is_relevant(html, "https://www.nseindia.com/static/complaints/defaulter-expelled-members",
                         None, no_text) == (False, "non_listing_page")
    assert p.is_relevant(pdf, "https://nsearchives.nseindia.com/x/SOP_for_Delisting_of_Equity_Shares_0.pdf",
                         None, no_text) == (False, "procedural_document")
    assert p.is_relevant(pdf, "https://nsearchives.nseindia.com/x/Jindal%20Cotex.pdf",
                         "https://www.nseindia.com/static/list/orders-of-delisting", no_text)[0]
    assert p.is_relevant(html, "https://www.nseindia.com/static/list/foo", None,
                         {"title": "Orders of Delisting", "text_head": ""})[0]
    # HTML bodies contain site-wide "Delisting" nav links: body text alone is not enough
    assert not p.is_relevant(html, "https://www.nseindia.com/static/list/foo", None,
                             {"title": "Listing compliance", "text_head": "menu: Delisting"})[0]
    assert p.is_relevant(pdf, "https://www.sebi.gov.in/x.pdf", None,
                         {"title": "", "text_head": "order regarding compulsory delisting"})[0]


def test_proposed_lists_are_detected():
    assert p.PROPOSED_RE.search("Companies_proposed_to_be_delisted_list.xlsx")
    assert not p.PROPOSED_RE.search("Delisting_Order-SECURCRED.pdf")


def test_listing_and_people_tables_are_rejected():
    listing = pd.DataFrame([["Company Name", "Symbol", "Date of Listing"], ["AXIS BANK LIMITED", "AXISBANK", "31-08-2016"]])
    people = pd.DataFrame([["Name", "Designation", "DIN"], ["Togo Shimizu", "Senior Managing Director", "0123"]])
    assert p.rows_from_table(listing, "OTHER") == []
    assert p.rows_from_table(people, "OTHER") == []


def test_name_needs_identifier_or_company_suffix():
    df = pd.DataFrame([["Name of the Company", "Date of Delisting"],
                       ["Parsoli Corporation", "01-02-2017"], ["Amber Zaidi", "01-02-2017"],
                       ["(a) The Discovered Price that is accepted by the Acquirer shall be final", "01-02-2017"]])
    assert [r["company_name"] for r in p.rows_from_table(df, "COMPULSORY")] == ["Parsoli Corporation"]


def test_plain_date_only_trusted_with_symbol():
    with_sym = pd.DataFrame([["Symbol", "Company Name", "Date"], ["ABC", "ABC Ltd", "05-06-2017"]])
    no_sym = pd.DataFrame([["Company Name", "Date", "Remarks"], ["ABC Ltd", "05-06-2017", "x"]])
    assert p.rows_from_table(with_sym, "OTHER")[0]["effective_delisting_date"] == "2017-06-05"
    assert p.rows_from_table(no_sym, "OTHER") == []


def test_wrapped_names_are_joined():
    df = pd.DataFrame([["Symbol", "Company Name", "Delisted Date"], ["GTNIND", "GTN Industries\nLimited", "2018-01-01"]])
    assert p.rows_from_table(df, "COMPULSORY")[0]["company_name"] == "GTN Industries Limited"


def test_symbol_keyed_table_with_promoter_column_is_kept():
    df = pd.DataFrame([["Sr.no", "Symbol", "Name of Company", "Name of the Promoter"],
                       [1, "ARCOTECH", "Arcotech Limited", "Arvind Kumar Saraf"]])
    assert [r["nse_symbol"] for r in p.rows_from_table(df, "OTHER")] == ["ARCOTECH"]


def test_order_without_symbol_in_matter_line_and_withdrawn_wording():
    text = """Before the Delisting Committee ("Committee")
In the matter of the company
M/s Petron Engineering Construction Limited
Members present:
... the Committee, directs that the dealings in the securities of the Company M/s Petron Engineering
Construction Limited (PETRONENGG) be restrained and withdrawn from the Exchange platform in terms of
Regulation 22 (1) of SEBI (Delisting of Equity Shares) Regulation, 2009.
Date: February 02,2023
"""
    [row] = p.rows_from_order(text)
    assert row["company_name"] == "Petron Engineering Construction Limited"
    assert row["nse_symbol"] == "PETRONENGG"
    assert row["delisting_type"] == "COMPULSORY"
    assert row["order_date"] == "2023-02-02"


def test_trading_member_orders_are_not_companies():
    text = """Member and Core Settlement Guarantee Fund Committee ("MCSGFC")
In the matter of the Trading Member M/s. GSV Securities Private Limited
... shall be expelled ..."""
    assert p.rows_from_order(text) == []


def test_isin_check_digit_rejects_sebi_registration_numbers():
    from common import isin_valid
    assert isin_valid("INE002A01018")          # Reliance Industries
    assert not isin_valid("INE002A01019")      # bad check digit
    assert not isin_valid("INM000010122")      # SEBI merchant-banker registration
    assert not isin_valid("INZ000206732")      # SEBI broker registration


def test_text_rows_skip_letterhead_labels():
    text = "SEBI Reg. No. INE002A01018 CIN U67120\nE-mail: cs@x.com | SEBI Reg. No. INE002A01018"
    assert p.rows_from_text(text, "OTHER") == []
