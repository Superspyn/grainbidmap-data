"""Adapter tests against real captured responses. No network access.

The fixtures are genuine responses saved from the live sources, so these tests
catch the thing most likely to break in production: an upstream markup change.
"""

import json
import pathlib

import pytest

from adapters.agricharts import AgriChartsAdapter
from adapters.heartland import HeartlandAdapter
from adapters.landus import LandusAdapter
from adapters.newcoop import NewCoopAdapter

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
