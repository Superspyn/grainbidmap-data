"""Adapter tests against real captured responses. No network access.

The fixtures are genuine responses saved from the live sources, so these tests
catch the thing most likely to break in production: an upstream markup change.
"""

import json
import pathlib

import pytest

from adapters.agricharts import AgriChartsAdapter
from adapters.heartland import HeartlandAdapter

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
