"""Adapter tests against real captured responses. No network access.

The fixtures are genuine responses saved from the live sources, so these tests
catch the thing most likely to break in production: an upstream markup change.
"""

import json
import pathlib

import pytest

from adapters.agricharts import AgriChartsAdapter
from adapters.cihedging import CIHedgingAdapter
from adapters.gradable import GradableAdapter
from adapters.heartland import HeartlandAdapter
from adapters.landus import LandusAdapter
from adapters.newcoop import NewCoopAdapter
from adapters.nexus import NexusAdapter

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip("missing fixture " + name)
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def nicoop():
    return AgriChartsAdapter("nicoop").parse(read_fixture("agricharts_nicoop.js"))


@pytest.fixture(scope="module")
def newcoop():
    return NewCoopAdapter().parse(read_fixture("newcoop_cash_bids.html"))


@pytest.fixture(scope="module")
def heartland():
    return HeartlandAdapter().parse(read_fixture("heartland_bids.htm"))


class TestAgriCharts:
    def test_parses_every_location(self, nicoop):
        assert len(nicoop) == 4
        assert {loc.name for loc in nicoop} == {
            "Thornton", "Portland", "Plymouth", "Clear Lake"
        }

    def test_publishes_coordinates(self, nicoop):
        # Coordinates are what make automatic pin matching trustworthy.
        for loc in nicoop:
            assert loc.latitude and 40 < loc.latitude < 44
            assert loc.longitude and -97 < loc.longitude < -90

    def test_has_both_grains(self, nicoop):
        grains = {b.grain for loc in nicoop for b in loc.bids}
        assert grains == {"corn", "soybeans"}

    def test_basis_is_derived_from_cash_minus_futures(self, nicoop):
        # The feed's own `basis` field is inconsistently scaled between tenants,
        # so the adapter derives it. Check the arithmetic actually holds.
        for loc in nicoop:
            for bid in loc.bids:
                if bid.futures is not None:
                    assert bid.basis == pytest.approx(bid.cash - bid.futures, abs=1e-4)

    def test_known_row_matches_the_source(self, nicoop):
        thornton = next(l for l in nicoop if l.name == "Thornton")
        aug_corn = next(
            b for b in thornton.bids
            if b.grain == "corn" and b.delivery_start == "2026-08-01"
        )
        # Source row: futures "511-0", cashpricebushel "$4.78", basis -33 cents.
        assert aug_corn.futures == pytest.approx(5.11)
        assert aug_corn.cash == pytest.approx(4.78)
        assert aug_corn.basis == pytest.approx(-0.33)
        assert aug_corn.futures_month == "CU26"

    def test_every_bid_has_a_cash_price(self, nicoop):
        for loc in nicoop:
            for bid in loc.bids:
                assert bid.cash is not None

    def test_raises_on_unrecognisable_payload(self):
        with pytest.raises(ValueError, match="no 'var bids"):
            AgriChartsAdapter("nicoop").parse("<html>Site Not Configured</html>")

    def test_raises_when_payload_has_no_corn_or_beans(self):
        payload = 'var bids = [{"id":"1","name":"X","cashbids":[]}];'
        with pytest.raises(ValueError, match="no corn or soybean"):
            AgriChartsAdapter("nicoop").parse(payload)


class TestCIHedging:
    """Golden Grain Energy. The endpoint returns table markup wrapped in a JSON
    string, and rows are attributed rather than positional."""

    @staticmethod
    @pytest.fixture(scope="class")
    def parsed():
        return CIHedgingAdapter.parse(
            read_fixture("cihedging_goldengrain.json"), "98951", "Golden Grain Energy"
        )

    def test_single_location(self, parsed):
        assert len(parsed) == 1
        assert parsed[0].name == "Golden Grain Energy"

    def test_corn_only(self, parsed):
        # An ethanol plant buys corn; a soybean row here would mean a mis-parse.
        assert {b.grain for b in parsed[0].bids} == {"corn"}

    def test_basis_and_futures_reconcile(self, parsed):
        for b in parsed[0].bids:
            if b.futures is not None and b.basis is not None:
                assert abs((b.futures + b.basis) - b.cash) <= 0.011

    def test_futures_month_from_spoken_contract(self, parsed):
        """The futures cell reads "Sep 26 5.1200" - month, year and price in one
        string, with no symbol anywhere."""
        months = {b.futures_month for b in parsed[0].bids}
        assert "CU26" in months and "CZ26" in months
        assert all(m and len(m) == 4 for m in months)

    def test_delivery_from_row_attributes(self, parsed):
        for b in parsed[0].bids:
            assert b.delivery_start and b.delivery_end
            assert b.delivery_start[:4].isdigit()

    def test_rejects_markup_with_no_bids(self):
        with pytest.raises(ValueError, match="no corn or soybean"):
            CIHedgingAdapter.parse('"<div>nothing here</div>"', "1", "x")


class TestGradable:
    """POET and ADM both run Gradable. Responses carry a while(1); XSSI guard
    that must be stripped before parsing."""

    @staticmethod
    @pytest.fixture(scope="class")
    def bids():
        import json, re
        raw = read_fixture("gradable_instruments_hanlontown.json")
        payload = json.loads(re.sub(r"^\s*while\(1\);\s*", "", raw))
        return GradableAdapter.parse_instruments(payload, {1: "corn", 2: "soybeans"})

    @pytest.mark.parametrize("raw", [
        'while(1);{"instruments": []}',      # bootstrap ships this XSSI guard
        '  while(1);  {"instruments": []}',
        '{"instruments": []}',               # instruments endpoint does not
    ])
    def test_strips_the_xssi_guard_when_present(self, raw):
        """Gradable prefixes some responses with while(1); to defeat JSON
        hijacking. Loading must cope whether or not it is there."""
        from adapters.gradable import _load
        assert _load(raw) == {"instruments": []}

    def test_parses_bids(self, bids):
        assert bids
        assert all(b.cash is not None for b in bids)

    def test_basis_and_futures_reconcile(self, bids):
        for b in bids:
            if b.futures is not None and b.basis is not None:
                # cash_bid is rounded half-down by Gradable, so allow a cent.
                assert abs((b.futures + b.basis) - b.cash) <= 0.011

    def test_single_digit_contract_year_expanded(self, bids):
        # option_month arrives as "ZCU6"; it must not stay a 1-digit year.
        months = [b.futures_month for b in bids if b.futures_month]
        assert months
        assert all(len(m) == 4 for m in months), months[:5]

    def test_epoch_delivery_dates_become_iso(self, bids):
        for b in bids:
            if b.delivery_start:
                assert len(b.delivery_start) == 10 and b.delivery_start[4] == "-"

    def test_unknown_commodity_ids_fall_back_to_row_codes(self):
        payload = {"instruments": [{"commodity_id": 999, "ext_commodity_id": "ZS",
                                    "cash_bid": 12.0, "option_month": "ZSX6",
                                    "delivery_period_start": 1785542400}]}
        out = GradableAdapter.parse_instruments(payload, {})
        assert len(out) == 1 and out[0].grain == "soybeans"


class TestLandus:
    """Landus publishes basis and the bid but no futures column, so futures is
    derived. It is also the only per-location source, so partial failure
    handling matters."""

    @staticmethod
    @pytest.fixture(scope="class")
    def parsed():
        return LandusAdapter.parse_location(read_fixture("landus_cash_bids_109.json"))

    def test_directory_covers_every_pin(self):
        import json
        d = json.loads(read_fixture("landus_locations.json"))
        assert len(d) == 51
        assert all(e.get("locationNumber") and e.get("locationName") for e in d)

    def test_both_grains(self, parsed):
        bids, _ = parsed
        assert {b.grain for b in bids} == {"corn", "soybeans"}

    def test_futures_derived_from_bid_minus_basis(self, parsed):
        bids, _ = parsed
        for b in bids:
            if b.basis is not None:
                assert b.futures == pytest.approx(b.cash - b.basis, abs=1e-4)

    def test_futures_month_built_from_basis_month(self, parsed):
        bids, _ = parsed
        corn = [b for b in bids if b.grain == "corn" and b.futures_month]
        beans = [b for b in bids if b.grain == "soybeans" and b.futures_month]
        assert corn and beans
        assert all(b.futures_month.startswith("C") for b in corn)
        assert all(b.futures_month.startswith("S") for b in beans)

    def test_single_delivery_month_gets_bounds(self, parsed):
        bids, _ = parsed
        for b in bids:
            assert b.delivery_start and b.delivery_end
            assert b.delivery_start <= b.delivery_end

    def test_as_of_converted_from_central_to_utc(self, parsed):
        _, as_of = parsed
        assert as_of and as_of.endswith("Z")

    def test_as_of_falls_back_when_unparseable(self):
        bids, as_of = LandusAdapter.parse_location('{"asOfDateTime":"garbage","cashBids":[]}')
        assert bids == []
        assert as_of and as_of.endswith("Z")


class TestNewCoop:
    def test_parses_every_town(self, newcoop):
        assert len(newcoop) == 76
        names = {l.name for l in newcoop}
        assert {"Afton", "Algona", "Anita", "Blencoe"} <= names

    def test_town_name_comes_from_the_preceding_heading(self, newcoop):
        # Each table's location is the nearest heading above it; a mix-up here
        # would silently attach one town's bids to another.
        assert newcoop[0].name == "Afton"

    def test_both_grains(self, newcoop):
        assert {b.grain for l in newcoop for b in l.bids} == {"corn", "soybeans"}

    def test_basis_reconciles(self, newcoop):
        for loc in newcoop:
            for bid in loc.bids:
                if bid.futures is not None:
                    assert bid.basis == pytest.approx(bid.cash - bid.futures, abs=1e-4)

    def test_parses_positive_futures_change(self, newcoop):
        """Guards the "+3-6" bug: soybeans were up the day this fixture was
        captured, and every change parsed as None before the fix."""
        changes = [b.futures_change for l in newcoop for b in l.bids
                   if b.grain == "soybeans" and b.futures_change is not None]
        assert changes, "no soybean futures_change parsed at all"
        assert any(c > 0 for c in changes)

    def test_delivery_ranges(self, newcoop):
        bid = newcoop[0].bids[0]
        assert bid.delivery_start and bid.delivery_end
        assert bid.delivery_start < bid.delivery_end

    def test_raises_on_a_page_with_no_tables(self):
        with pytest.raises(ValueError, match="no corn or soybean"):
            NewCoopAdapter().parse("<html><body><h1>Afton</h1></body></html>")


class TestHeartland:
    def test_parses_all_four_tables(self, heartland):
        assert len(heartland) == 74
        # Regular and processor delivery points are kept distinct.
        assert any(l.source_location_id.startswith("processor:") for l in heartland)
        assert any(not l.source_location_id.startswith("processor:") for l in heartland)

    def test_reads_the_two_span_cash_and_basis_cell(self, heartland):
        alleman = next(l for l in heartland if l.name == "ALLEMAN")
        aug_corn = next(
            b for b in alleman.bids
            if b.grain == "corn" and b.delivery_start == "2026-08-01"
        )
        # Source cell: <span>4.80</span><span>-0.34</span>
        assert aug_corn.cash == pytest.approx(4.80)
        assert aug_corn.basis == pytest.approx(-0.34)
        assert aug_corn.futures_month == "CU26"

    def test_futures_derived_from_cash_minus_basis(self, heartland):
        alleman = next(l for l in heartland if l.name == "ALLEMAN")
        aug_corn = next(
            b for b in alleman.bids
            if b.grain == "corn" and b.delivery_start == "2026-08-01"
        )
        assert aug_corn.futures == pytest.approx(4.80 - (-0.34))

    def test_futures_are_consistent_across_locations(self, heartland):
        """Every location shares one futures market, so a given contract month
        must derive to the same futures price everywhere. This is the strongest
        available check that the cash/basis columns are not swapped."""
        by_month: dict[str, set] = {}
        for loc in heartland:
            for bid in loc.bids:
                if bid.futures_month and bid.futures is not None:
                    by_month.setdefault(bid.futures_month, set()).add(round(bid.futures, 2))
        for month, values in by_month.items():
            assert len(values) == 1, month + " derived inconsistent futures: " + str(values)

    def test_both_grains_present(self, heartland):
        grains = {b.grain for loc in heartland for b in loc.bids}
        assert grains == {"corn", "soybeans"}

    def test_as_of_uses_the_published_close(self, heartland):
        # Header reads "CLOSING GRAIN BIDS 82626" and the page states 1:15 PM CT.
        assert heartland[0].as_of == "2026-08-26T18:15:00Z"

    def test_raises_on_a_page_with_no_bid_tables(self):
        with pytest.raises(ValueError, match="no corn or soybean"):
            HeartlandAdapter().parse("<html><body><p>Down for maintenance</p></body></html>")


class TestBuiltOutput:
    """Checks on the committed docs/bids.json, if one has been built."""

    @staticmethod
    @pytest.fixture(scope="class")
    def built():
        path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "bids.json"
        if not path.exists():
            pytest.skip("docs/bids.json not built yet")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_no_null_cash_prices(self, built):
        for pin_id, loc in built["locations"].items():
            for bid in loc["bids"]:
                assert bid.get("cash") is not None, pin_id

    def test_only_corn_and_soybeans(self, built):
        grains = {b["grain"] for l in built["locations"].values() for b in l["bids"]}
        assert grains <= {"corn", "soybeans"}

    def test_every_location_has_bids(self, built):
        for pin_id, loc in built["locations"].items():
            assert loc["bids"], pin_id

    def test_carries_a_disclaimer(self, built):
        assert "reference only" in built["disclaimer"]


class TestDedupeBids:
    """Only fully identical rows collapse - a shared window is not enough."""

    def _bid(self, **over):
        base = {
            "grain": "soybeans", "delivery_start": "2026-08-01",
            "delivery_end": "2026-08-31", "delivery_label": "Aug 2026",
            "futures_month": "SX26", "futures": 12.79, "futures_change": 0.11,
            "basis": 0.0, "cash": 12.79,
        }
        base.update(over)
        return base

    def test_identical_rows_collapse(self):
        from build_bids import dedupe_bids
        assert len(dedupe_bids([self._bid(), self._bid()])) == 1

    def test_same_window_different_basis_is_kept(self):
        # CVA Monroe quotes two bids for Jul/Aug 2026 that differ only in basis.
        # That is the merchandiser drawing a real distinction, not a duplicate.
        from build_bids import dedupe_bids
        rows = [self._bid(basis=-0.2675, cash=4.87), self._bid(basis=0.6925, cash=5.83)]
        assert len(dedupe_bids(rows)) == 2

    def test_order_is_preserved(self):
        from build_bids import dedupe_bids
        rows = [self._bid(cash=1.0), self._bid(cash=2.0), self._bid(cash=1.0)]
        assert [b["cash"] for b in dedupe_bids(rows)] == [1.0, 2.0]


@pytest.fixture(scope="module")
def cargill():
    return AgriChartsAdapter("cargillus").parse(read_fixture("agricharts_cargillus.js"))


class TestAgriChartsBasisDrivenMode:
    """Cargill's feed (price_calculations = 2) inverts what is authoritative.

    Its price fields are not a bid: every location carries the same rounded
    board price, so deriving basis from them gives ~0 everywhere. The real,
    location-specific number is `basis`, in dollars, and the bid is
    futures + basis. Getting this backwards published Alberta at $5.12 when
    the actual bid was $4.47.
    """

    def test_basis_comes_from_the_feed_in_dollars(self, cargill):
        alberta = next(l for l in cargill if l.name == "Alberta, CAH")
        assert alberta.bids[0].basis == -0.65

    def test_cash_is_futures_plus_basis_not_the_price_field(self, cargill):
        alberta = next(l for l in cargill if l.name == "Alberta, CAH")
        bid = alberta.bids[0]
        assert bid.futures == 5.1275          # "512-6"
        assert bid.cash == 4.4775             # not $5.12 from cashprice
        assert round(bid.futures + bid.basis, 4) == bid.cash

    def test_basis_varies_by_location_on_the_same_futures(self, cargill):
        # Minnesota interior vs an Illinois river terminal, same board month.
        alberta = next(l for l in cargill if l.name == "Alberta, CAH").bids[0]
        beardstown = next(l for l in cargill if l.name == "Beardstown, CAH").bids[0]
        assert alberta.futures == beardstown.futures
        assert alberta.basis == -0.65 and beardstown.basis == -0.05
        assert alberta.cash < beardstown.cash

    def test_positive_basis_survives(self, cargill):
        beans = next(
            b
            for l in cargill
            if l.name == "Beardstown, CAH"
            for b in l.bids
            if b.grain == "soybeans"
        )
        assert beans.basis == 0.15
        assert beans.cash == 12.94            # 12.79 + 0.15

    def test_coop_feeds_are_unaffected(self, nicoop):
        # Mode 0: price is the bid and basis is derived. Guards against the
        # fix leaking into the 300+ pins that were always right.
        for loc in nicoop:
            for bid in loc.bids:
                if bid.basis is not None and bid.futures is not None:
                    assert abs(bid.futures + bid.basis - bid.cash) < 0.006


@pytest.fixture(scope="module")
def nexus():
    return NexusAdapter.parse(read_fixture("nexus_cash_bids.html"))


class TestNexus:
    def test_reads_locations_by_name(self, nexus):
        assert "Rockford, IA" in {loc.name for loc in nexus}

    def test_columns_are_read_by_header_name(self, nexus):
        bid = next(b for l in nexus if l.name == "Rockford, IA" for b in l.bids)
        assert bid.grain == "corn"
        assert bid.futures == 5.15
        assert bid.basis == -0.38
        assert bid.cash == 4.77
        assert bid.futures_month == "CU26"

    def test_every_bid_reconciles(self, nexus):
        for loc in nexus:
            for b in loc.bids:
                if b.basis is not None and b.futures is not None:
                    assert abs(b.futures + b.basis - b.cash) < 0.006

    def test_location_with_no_quotes_is_dropped(self, nexus):
        # Conger's corn table says "No bids returned" and its beans carry a
        # blank basis with a bare "$" for cash. A location with nothing
        # quotable should not appear at all rather than appear empty.
        assert "Conger, MN" not in {loc.name for loc in nexus}

    def test_delivered_bids_stay_under_nexus(self, nexus):
        # Nexus quotes delivery to other companies' plants. The bid is Nexus's
        # own, not Cargill's, so it must be reported here - and company scoping
        # in match_locations keeps it off the Cargill pin.
        loc = next(l for l in nexus if l.name == "Cargill Iowa Falls, IA")
        assert loc.bids


class TestNexusChangeSign:
    """The futures change carries its sign in a CSS class, not the text.

    Every cell was class="pos" when this was built and the archived copies do
    not include the tables, so the negative rendering has never been observed.
    These snippets are synthetic - they exist to pin the behaviour whichever
    way Nexus renders a down day.
    """

    def _change(self, html):
        from bs4 import BeautifulSoup
        from adapters.nexus import _signed_change
        return _signed_change(BeautifulSoup(html, "html.parser").find("td"))

    def test_pos_class_is_positive(self):
        assert self._change('<td class="pos">0.0475</td>') == 0.0475

    def test_neg_class_negates_unsigned_text(self):
        assert self._change('<td class="neg">0.0475</td>') == -0.0475

    def test_explicit_sign_in_text_wins(self):
        assert self._change('<td class="neg">-0.0475</td>') == -0.0475
        assert self._change('<td class="pos">+0.0475</td>') == 0.0475

    def test_no_class_falls_back_to_text(self):
        assert self._change('<td>-0.02</td>') == -0.02
        assert self._change('<td>0.02</td>') == 0.02

    def test_unparseable_is_none(self):
        assert self._change('<td class="pos"></td>') is None
