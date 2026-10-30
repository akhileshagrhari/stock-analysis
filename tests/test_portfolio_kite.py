"""The Kite holdings parser and the lot-combining rule."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from stockanalysis.portfolio.kite import (
    MAX_BYTES,
    MAX_ROWS,
    ImportRefused,
    ParsedRow,
    parse_kite_holdings,
)
from stockanalysis.portfolio.lots import combine

FIXTURE = Path(__file__).parent / "fixtures" / "portfolio" / "kite_holdings.csv"
HEADER = '"Instrument","Qty.","Avg. cost","LTP"'


def test_combine_weights_the_cost():
    assert combine(Decimal(10), Decimal(100), Decimal(30), Decimal(200)) == (
        Decimal(40),
        Decimal("175.0000"),
    )


def test_reads_the_fixture():
    f = parse_kite_holdings(FIXTURE.read_bytes())
    assert f.format == "kite_csv"
    assert len(f.rows) == 23
    assert f.rows[0] == ParsedRow(
        "BAJFINANCE", Decimal(30), Decimal("2846.2"), Decimal("2778.96")
    )
    assert {r.symbol for r in f.rows} >= {"EMBASSY-RR", "SGBJUN31I", "NIFTYBEES", "TMPV"}


def test_survives_bom_crlf_blank_lines_quotes_and_thousands():
    text = f'{HEADER}\r\n\r\n"TCS","1,000","2,474.50","2,225.25"\r\n,,,\r\n\r\n'
    f = parse_kite_holdings(b"\xef\xbb\xbf" + text.encode())
    assert f.rows == [
        ParsedRow("TCS", Decimal(1000), Decimal("2474.50"), Decimal("2225.25"))
    ]


def test_blank_ltp_is_none():
    f = parse_kite_holdings(f"{HEADER}\nTCS,1,10,\n".encode())
    assert f.rows[0].file_price is None


def test_ltp_column_is_optional():
    f = parse_kite_holdings(b"Instrument,Qty.,Avg. cost\nTCS,1,10\n")
    assert f.rows[0].file_price is None


def test_columns_found_by_name_not_position():
    data = b'Extra,  avg. COST ,LTP,qty.,instrument\nx,10,11,2,INFY\n'
    f = parse_kite_holdings(data)
    assert f.rows == [ParsedRow("INFY", Decimal(2), Decimal(10), Decimal(11))]


def test_duplicate_symbols_are_combined():
    data = f"{HEADER}\nTCS,10,100,50\ntcs ,30,200,60\n".encode()
    f = parse_kite_holdings(data)
    assert f.rows == [
        ParsedRow("TCS", Decimal(40), Decimal("175.0000"), Decimal(60))
    ]


def test_missing_required_column_is_refused():
    with pytest.raises(ImportRefused, match="isn't a Kite holdings file"):
        parse_kite_holdings(b"Instrument,Qty.,LTP\nTCS,1,2\n")


@pytest.mark.parametrize(
    "data", [b"", b"PK\x03\x04\x00\xff\xfe\x80binary", b"Trade date,Symbol,Quantity\n"]
)
def test_wrong_files_are_refused(data):
    with pytest.raises(ImportRefused, match="isn't a Kite holdings file"):
        parse_kite_holdings(data)


def test_header_only_file_is_refused():
    with pytest.raises(ImportRefused, match="no holdings"):
        parse_kite_holdings(f"{HEADER}\n".encode())


def test_bad_rows_are_named():
    data = f"{HEADER}\nA,0,10,1\nB,abc,10,1\nC,1,-5,1\n".encode()
    with pytest.raises(ImportRefused) as e:
        parse_kite_holdings(data)
    assert e.value.problems == [
        "Row 2: quantity must be more than 0.",
        "Row 3: quantity isn't a number.",
        "Row 4: average cost can't be negative.",
    ]


def test_problems_are_capped_at_five():
    data = (HEADER + "\n" + "A,0,10,1\n" * 8).encode()
    with pytest.raises(ImportRefused) as e:
        parse_kite_holdings(data)
    assert len(e.value.problems) == 6
    assert e.value.problems[-1] == "…and 3 more."


def test_limits():
    with pytest.raises(ImportRefused, match="The file is larger than 1 MB."):
        parse_kite_holdings(b"x" * (MAX_BYTES + 1))
    rows = "".join(f"S{i},1,1,1\n" for i in range(MAX_ROWS + 1))
    with pytest.raises(ImportRefused, match="more than 2,000 holdings"):
        parse_kite_holdings((HEADER + "\n" + rows).encode())


@pytest.mark.parametrize(
    "row, problem",
    [
        ("A,1e30,10,1", "Row 2: quantity isn't a number."),
        ("A,1" + "0" * 400 + ",10,1", "Row 2: quantity is too large."),
        ("A,10000000000000000,10,1", "Row 2: quantity is too large."),
        ("A,1,1" + "0" * 400 + ",1", "Row 2: average cost is too large."),
        ("A,1,10,1" + "0" * 400, "Row 2: last price is too large."),
        ("A,1,1e3,1", "Row 2: average cost isn't a number."),
        ("A,1,10,NaN", "Row 2: last price isn't a number."),
        ("A,1,Infinity,1", "Row 2: average cost isn't a number."),
    ],
)
def test_huge_and_exponent_values_are_refused(row, problem):
    with pytest.raises(ImportRefused) as e:
        parse_kite_holdings(f"{HEADER}\n{row}\n".encode())
    assert e.value.problems == [problem]


def test_just_under_the_bound_is_accepted():
    f = parse_kite_holdings(f"{HEADER}\nA,9999999999999999.9999,1,1\n".encode())
    assert f.rows[0].quantity == Decimal("9999999999999999.9999")


def test_duplicates_that_add_past_the_bound_are_refused():
    big = "9999999999999999"
    with pytest.raises(ImportRefused) as e:
        parse_kite_holdings(f"{HEADER}\nA,{big},1,1\nA,{big},1,1\n".encode())
    assert e.value.problems == ["Row 3: A adds up to too large a holding."]
