"""Parsing the Iowa Department of Agriculture's month-end basis tables."""

import bid_history
import iowa_basis_history as h

PAGE_2006 = """IOWA DAILY CORN BASIS FOR NOVEMBER 2006
Basis
NW - I NC - II NE - III SW - IV SC - V SE - VI Average
1 -0.35 -0.35 -0.36 -0.37 -0.35 -0.31 -0.35
4
5
6 -0.35 -0.34 -0.40 -0.39 -0.34 -0.33 -0.36
-0.33 -0.33 -0.37 -0.38 -0.33 -0.30 -0.34
Basis determined by using December contract.
"""

PAGE_2015 = """IOWA DAILY SOYBEAN BASIS FOR MARCH 2015
NW - I NC - II NE - III SW - IV SC - V SE - VI Average
2 -0.23 -0.22 -0.20 -0.31 -0.27 -0.27 -0.24
Mo Avg -0.20 -0.20 -0.16 -0.27 -0.24 -0.20 -0.20
Basis determined by using (MAY) contract.
"""

PRICES_2025 = """Iowa State Average Corn Prices for October 2025
NW - I NC - II NE - III SW - IV SC - V SE - VI Average
1 3.7401 3.7260 3.7139 3.7236 3.7164 3.6200 3.7137
"""


def test_reads_each_district_and_skips_weekends_and_averages():
    rows = h.parse_page(PAGE_2006)
    nc = [(d, v) for d, dist, g, kind, v, c in rows if dist == "NC"]
    assert nc == [("2006-11-01", -0.35), ("2006-11-06", -0.34)]
    assert {r[5] for r in rows} == {"DEC"}
    assert {r[2] for r in rows} == {"corn"}
    assert len(rows) == 2 * 7          # six districts plus the state average


def test_parenthesised_contract_and_mo_avg_row():
    rows = h.parse_page(PAGE_2015)
    assert len(rows) == 7
    assert rows[0][2] == "soybeans" and rows[0][5] == "MAY"


def test_price_pages_are_cash():
    rows = h.parse_page(PRICES_2025)
    assert ("2025-10-01", "NC", "corn", "cash", 3.726, None) in rows


def test_a_typo_year_takes_the_reports_own_year():
    rows = h.parse_page(PAGE_2006.replace("2006", "2906"), file_year=2006)
    assert rows[0][0] == "2006-11-01"


def test_impossible_values_are_blanked(tmp_path, monkeypatch):
    db = bid_history.connect(tmp_path / "h.sqlite")
    bad = PRICES_2025.replace("3.7260", "0.0000") + "\n" + \
        "Iowa Daily Corn Basis for October 2025\n" \
        "1 -0.42 -0.44 -0.45 -0.44 -0.45 -0.55 -0.45\n" \
        "2 -0.42 6.98 -0.45 -0.44 -0.45 -0.55 -0.45\n" \
        "Basis determined by using December contract.\n"

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Reader:
        def __init__(self, path):
            self.pages = [Page(t) for t in bad.split("\n\n")]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    report = tmp_path / "2025" / "x.pdf"
    report.parent.mkdir()
    report.write_bytes(b"")
    h.import_files(db, [report])
    got = dict(((d, dist), (cash, basis)) for d, dist, cash, basis in db.execute(
        "SELECT day, district, cash, basis FROM iowa_district WHERE district IN ('NC', 'NW')"))
    assert got[("2025-10-01", "NC")] == (None, None)        # zero price: both blanked
    assert got[("2025-10-02", "NC")] == (None, None)        # 6.98 is a price, not a basis
    assert got[("2025-10-01", "NW")] == (3.7401, -0.42)
