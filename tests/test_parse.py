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
        [1, "ABC Industries Ltd", "abcind", "INE123A01016", "05-06-2017", "Compulsory delisting"],
        [2, "XYZ Ltd", "XYZ", "INE999Z01011", "12/09/2020", "NCLT resolution plan"],
        [3, "NaT", None, None, None, None],
    ])
    rows = p.rows_from_table(df, "OTHER")
    assert [r["company_name"] for r in rows] == ["ABC Industries Ltd", "XYZ Ltd"]
    assert rows[0]["nse_symbol"] == "ABCIND"
    assert rows[0]["effective_delisting_date"] == "2017-06-05"
    assert rows[0]["delisting_type"] == "COMPULSORY"
    assert rows[1]["delisting_type"] == "IBC_NCLT"


def test_text_rows_take_symbol_after_isin():
    [row] = p.rows_from_text("1. ABC Industries Limited INE123A01016 ABCIND 08 June 2017", "COMPULSORY")
    assert row["isin"] == "INE123A01016"
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
