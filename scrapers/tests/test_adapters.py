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
    return HeartlandAdapter().parse(read_fixture("heartland_sheet001.htm"))


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
    """Heartland publishes an Excel "Save as Web Page" export in a frameset.

    bids.htm is only the frameset now; the data is in bids_files/sheet001.htm,
    laid out as four blocks (CORN, SOYBEANS, DIRECT CORN, DIRECT SOYBEANS) with
    cash and basis alternating across the columns.
    """

    def test_parses_every_block(self, heartland):
        # Well above the forty-six rows of the first block, because soybeans
        # and both DIRECT blocks are read too, and combined towns are split.
        assert len(heartland) > 70
        grains = {b.grain for loc in heartland for b in loc.bids}
        assert grains == {"corn", "soybeans"}

    def test_reads_the_cash_and_basis_pair(self, heartland):
        cb = next(l for l in heartland if l.name == "COUNCIL BLUFFS")
        sep = next(b for b in cb.bids
                   if b.grain == "corn" and b.delivery_start == "2026-09-01")
        assert sep.cash == pytest.approx(5.06)
        assert sep.basis == pytest.approx(-0.37)
        assert sep.futures_month == "CZ26"

    def test_futures_derived_from_cash_minus_basis(self, heartland):
        for loc in heartland:
            for b in loc.bids:
                if b.basis is not None and b.futures is not None:
                    assert abs(b.futures - (b.cash - b.basis)) < 0.006

    def test_futures_are_consistent_across_locations(self, heartland):
        """Every location shares one futures market, so a given contract month
        must derive to the same futures price everywhere. This is the strongest
        available check that the cash and basis columns are not swapped."""
        by_month: dict[str, set] = {}
        for loc in heartland:
            for bid in loc.bids:
                if bid.futures_month and bid.futures is not None:
                    by_month.setdefault(bid.futures_month, set()).add(round(bid.futures, 2))
        for month, values in by_month.items():
            assert len(values) == 1, month + " derived inconsistent futures: " + str(values)

    def test_combined_towns_are_split(self, heartland):
        """The sheet quotes one row for "Minburn/Dallas Center" where the old
        page listed each town. Both ids are already mapped to pins, so both
        must still appear, carrying the same bids."""
        names = {l.source_location_id for l in heartland}
        for town in ("MINBURN", "DALLAS CENTER", "SLATER", "CAMBRIDGE",
                     "JEWELL", "RANDALL", "WAUKEE", "REDFIELD"):
            assert town in names, town
        minburn = next(l for l in heartland if l.name == "MINBURN")
        dallas = next(l for l in heartland if l.name == "DALLAS CENTER")
        assert [b.cash for b in minburn.bids] == [b.cash for b in dallas.bids]

    def test_renamed_town_keeps_its_old_id(self, heartland):
        # The sheet now spells out "Missouri Valley"; the pin is mapped to the
        # old "MO VALLEY", so both are emitted.
        names = {l.source_location_id for l in heartland}
        assert "MO VALLEY" in names
        assert "MISSOURI VALLEY" in names

    def test_summary_rows_are_not_locations(self, heartland):
        """Each block ends with an "Average" row, which is not a delivery
        point and would otherwise become a pinnable location."""
        for loc in heartland:
            assert not loc.name.upper().startswith(("AVERAGE", "AVE ", "LOCATION"))

    def test_every_bid_has_a_delivery_window(self, heartland):
        """The window row reads "09/01/26 - 09/30/26". An earlier regex was
        written against a truncated debug print and expected no year on the
        end date, so it matched nothing and every bid lost its dates."""
        for loc in heartland:
            for b in loc.bids:
                assert b.delivery_start and b.delivery_end
                assert b.delivery_end >= b.delivery_start

    def test_raises_on_a_page_with_no_bid_tables(self):
        with pytest.raises(ValueError):
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


@pytest.fixture(scope="module")
def bushel():
    from adapters.bushel import BushelAdapter
    return BushelAdapter.parse(read_fixture("bushel_bids.json"), "test")


class TestBushel:
    """CHS/AGP/Smithfield via Bushel's aggregator API. Clean JSON, but three
    traps: free-text delivery labels, an out-of-band futures change sign, and
    flat rows carrying a literal "0.00" price."""

    def test_verified_against_agps_own_page(self, bushel):
        # The live API was checked against agp.com/bids/ rendered in a browser
        # the same day (August 13.06 / By September 15 12.86 - exact match at
        # fetch time). This fixture is a snapshot from ~2h later, one tick
        # lower; the STRUCTURE is what the test pins, on real values.
        eg = next(l for l in bushel if l.name == "Eagle Grove, IA")
        by_label = {b.delivery_label: b for b in eg.bids}
        assert by_label["August"].cash == 13.05
        assert by_label["August"].basis == 0.2
        assert by_label["By September 15"].cash == 12.85
        assert by_label["By September 30"].cash == 12.50

    def test_futures_symbol_becomes_month_code(self, bushel):
        eg = next(l for l in bushel if l.name == "Eagle Grove, IA")
        assert {b.futures_month for b in eg.bids} >= {"SX26", "SF27"}

    def test_wheat_is_dropped(self, bushel):
        mitchell = next(l for l in bushel if l.name == "Mitchell")
        assert {b.grain for b in mitchell.bids} <= {"corn", "soybeans"}

    def test_every_bid_reconciles(self, bushel):
        # CHS truncates the published bid down to the whole cent while futures
        # keeps quarter-cents (5.3375 - 0.65 = 4.6875, shown as 4.68), so
        # allow a cent - same as Gradable's half-down rounding.
        for loc in bushel:
            for b in loc.bids:
                if b.basis is not None and b.futures is not None:
                    assert abs(b.futures + b.basis - b.cash) <= 0.011

    def test_zero_price_flat_row_is_dropped(self, bushel):
        # Alma's only corn/bean row is a flat bid with bidPrice "0.00" -
        # a placeholder, not a bid. The location must vanish entirely.
        assert "Alma" not in {l.name for l in bushel}

    def test_floating_rows_are_dropped(self, bushel):
        # CHS Primeland's row is bidType=floating (and wheat besides).
        assert "CHS Primeland" not in {l.name for l in bushel}


class TestBushelDeliveryLabels:
    def _p(self, label):
        from adapters.bushel import parse_delivery
        return parse_delivery(label)

    def test_month_year(self):
        assert self._p("AUG 2026") == ("2026-08-01", "2026-08-31")
        assert self._p("Sept 2026") == ("2026-09-01", "2026-09-30")
        assert self._p("March 2027") == ("2027-03-01", "2027-03-31")
        assert self._p("SEPT. 26") == ("2026-09-01", "2026-09-30")

    def test_month_ranges(self):
        assert self._p("OCT/NOV 2026") == ("2026-10-01", "2026-11-30")
        assert self._p("O/N 2026") == ("2026-10-01", "2026-11-30")

    def test_range_wrapping_the_year(self):
        assert self._p("DEC/JAN 2026") == ("2026-12-01", "2027-01-31")

    def test_unparseable_labels_stay_labels(self):
        assert self._p("New Crop 2026") == (None, None)
        assert self._p("Open Storage") == (None, None)
        assert self._p("August") == (None, None)       # no year - do not guess
        assert self._p("By September 15") == (None, None)


class TestBushelChangeSign:
    """futuresChangeSign is 1 on every observed row; -1 has never been seen
    live. Synthetic rows pin the behaviour for a down day."""

    def _bid(self, **over):
        from adapters.bushel import BushelAdapter
        row = {"bidType": "cash", "description": "AUG 2026", "bidPrice": "4.50",
               "basisPrice": "-0.50", "futuresPrice": "5.00",
               "futuresChange": "0.0250", "futuresChangeSign": 1,
               "futuresSymbol": "ZCZ26"}
        row.update(over)
        return BushelAdapter._parse_bid(row, "corn")

    def test_sign_one_stays_positive(self):
        assert self._bid().futures_change == 0.025

    def test_sign_minus_one_negates(self):
        assert self._bid(futuresChangeSign=-1).futures_change == -0.025

    def test_explicit_sign_in_text_wins(self):
        assert self._bid(futuresChange="-0.0250", futuresChangeSign=-1).futures_change == -0.025

    def test_null_change_stays_null(self):
        assert self._bid(futuresChange=None, futuresChangeSign=None).futures_change is None


@pytest.fixture(scope="module")
def fivestar():
    from adapters.fivestar import FiveStarAdapter
    return FiveStarAdapter.parse(read_fixture("fivestar_cash_bids.html"))


class TestFiveStar:
    def test_reads_every_location(self, fivestar):
        names = {loc.name for loc in fivestar}
        # Their fourteen elevators, plus the plants they quote delivery to.
        for town in ("Burchinal", "Hanlontown", "Klemme", "Lime Springs",
                     "North Washington", "Ventura", "Rockwell", "Ionia"):
            assert town in names, town

    def test_keyed_by_the_feeds_own_location_id(self, fivestar):
        # The name is not unique across their whole feed and could be
        # retitled; LocationID is the stable handle.
        loc = next(l for l in fivestar if l.name == "Ionia")
        assert loc.source_location_id == "7TZJ7E2012ZHMUBKRLVJ"

    def test_futures_is_derived_from_cash_and_basis(self, fivestar):
        # The feed publishes no futures price, so every row's must reconcile
        # by construction - this pins the arithmetic, not the source.
        for loc in fivestar:
            for b in loc.bids:
                assert b.basis is not None and b.futures is not None
                assert abs(b.futures + b.basis - b.cash) < 0.006

    def test_change_is_converted_from_cents(self, fivestar):
        # The column reads "3" for a three cent day, matching the board table.
        loc = next(l for l in fivestar if l.name == "Klemme")
        bid = next(b for b in loc.bids if b.futures_month == "CZ26")
        assert bid.futures_change == 0.03

    def test_every_row_resolves_to_one_contract(self, fivestar):
        # Price alone is ambiguous (CZ26 and CH28 both closed at 5.365) and so
        # is change; together they resolved all 291 rows when this was built.
        unresolved = [b for l in fivestar for b in l.bids if b.futures_month is None]
        assert unresolved == []

    def test_delivered_bids_stay_under_five_star(self, fivestar):
        # These are Five Star's bids for grain hauled to those plants, not the
        # plants' own posted bids. They belong in this source; company scoping
        # in match_locations keeps them off the AGP and Valero pins.
        names = {loc.name for loc in fivestar}
        assert "AGP Mason City" in names
        assert "Valero Charles City" in names

    def test_blank_rows_are_dropped(self, fivestar):
        # A period a location is not bidding on is left blank, not omitted.
        for loc in fivestar:
            for b in loc.bids:
                assert b.cash is not None and b.cash > 0


class TestFiveStarDeliveryPeriods:
    """Delivery periods are free text and inconsistently written."""

    def _window(self, text):
        from adapters.fivestar import _delivery_window
        return _delivery_window(text)

    def test_whole_month_spellings(self):
        assert self._window("Dec26") == ("2026-12-01", "2026-12-31")
        assert self._window("Oct 2026.") == ("2026-10-01", "2026-10-31")
        assert self._window("Aug 26.") == ("2026-08-01", "2026-08-31")
        assert self._window("April 27") == ("2027-04-01", "2027-04-30")
        assert self._window("July 27") == ("2027-07-01", "2027-07-31")

    def test_half_months(self):
        assert self._window("FH Sep")[0].endswith("-09-01")
        assert self._window("FH Sep")[1].endswith("-09-15")
        assert self._window("LH Aug")[0].endswith("-08-16")
        assert self._window("LH Aug")[1].endswith("-08-31")

    def test_february_end_is_not_hardcoded(self):
        assert self._window("Feb 28") == ("2028-02-01", "2028-02-29")
        assert self._window("Feb 27") == ("2027-02-01", "2027-02-28")

    def test_junk_is_none(self):
        assert self._window("") == (None, None)
        assert self._window("Delivery Periods") == (None, None)


class TestFiveStarContractMatch:
    """Two contracts can share a close, and two can share a day's change."""

    def _board(self):
        from adapters.fivestar import _Board
        return _Board([
            ("corn", 5.365, 0.03, "CZ26"),
            ("corn", 5.365, 0.015, "CH28"),    # same close, different change
            ("corn", 5.57, 0.04, "CK27"),
            ("corn", 5.575, 0.0325, "CN27"),   # half a cent from CK27
        ])

    def test_change_breaks_a_price_tie(self):
        assert self._board().match("corn", 5.36, 0.03) == "CZ26"
        assert self._board().match("corn", 5.36, 0.015) == "CH28"

    def test_price_breaks_a_change_tie(self):
        assert self._board().match("corn", 5.57, 0.04) == "CK27"
        assert self._board().match("corn", 5.57, 0.0325) == "CN27"

    def test_no_guess_when_still_ambiguous(self):
        from adapters.fivestar import _Board
        board = _Board([("corn", 5.365, 0.03, "CZ26"), ("corn", 5.365, 0.03, "CH28")])
        assert board.match("corn", 5.36, 0.03) is None

    def test_no_match_is_none_not_an_error(self):
        assert self._board().match("corn", 9.99, 0.03) is None
        assert self._board().match("corn", None, 0.03) is None
        assert self._board().match("soybeans", 5.36, 0.03) is None


class TestMislabelledBidGuard:
    """One bad row ranks first in a "best bids" table, so it matters most.

    Gold Eagle publishes a row with the malformed symbol "ZCZ2Z": it classifies
    as corn off the ZC root but carries a soybean price and no basis or
    futures, which made it the best corn bid in the state.
    """

    def _locations(self, extra=None):
        from build_bids import drop_mislabelled_bids
        # A realistic spread so the median and MAD are meaningful.
        locs = {}
        for i in range(60):
            locs[f'corn-{i}'] = {'bids': [
                {'grain': 'corn', 'cash': 4.80 + (i % 20) * 0.05, 'delivery_label': 'Oct 2026'}]}
            locs[f'bean-{i}'] = {'bids': [
                {'grain': 'soybeans', 'cash': 12.10 + (i % 20) * 0.06, 'delivery_label': 'Oct 2026'}]}
        if extra:
            locs.update(extra)
        return locs, drop_mislabelled_bids

    def test_soybean_price_filed_as_corn_is_dropped(self):
        locs, drop = self._locations({'bad': {'bids': [
            {'grain': 'corn', 'cash': 12.373, 'futures_month': 'ZCZ2Z',
             'delivery_label': 'Aug 2026'}]}})
        dropped = drop(locs)
        assert len(dropped) == 1
        assert 'bad' in dropped[0]
        assert locs['bad']['bids'] == []

    def test_a_genuinely_high_corn_bid_survives(self):
        # The best real corn bid in the set, not an error.
        locs, drop = self._locations({'high': {'bids': [
            {'grain': 'corn', 'cash': 5.95, 'delivery_label': 'Oct 2026'}]}})
        assert drop(locs) == []
        assert len(locs['high']['bids']) == 1

    def test_normal_rows_are_untouched(self):
        locs, drop = self._locations()
        assert drop(locs) == []
        assert all(len(v['bids']) == 1 for v in locs.values())

    def test_too_few_rows_to_judge_leaves_everything(self):
        from build_bids import drop_mislabelled_bids
        # Below the sample floor nothing is characterised, so nothing is cut.
        locs = {'a': {'bids': [{'grain': 'corn', 'cash': 5.0}]},
                'b': {'bids': [{'grain': 'corn', 'cash': 99.0}]}}
        assert drop_mislabelled_bids(locs) == []
        assert len(locs['b']['bids']) == 1

    def test_missing_cash_is_not_treated_as_an_outlier(self):
        locs, drop = self._locations({'nocash': {'bids': [
            {'grain': 'corn', 'cash': None, 'delivery_label': 'Oct 2026'}]}})
        assert drop(locs) == []
