"""Tests for the value parsing that everything else depends on.

Tick notation is where a silent bug would do the most damage: misreading
"1259-6" as 12.59 instead of 12.5975 shifts a soybean bid by three quarters of a
cent per bushel, which is wrong but not obviously wrong.
"""

import pytest

from normalize import (
    CORN,
    SOYBEANS,
    classify_grain,
    format_delivery_label,
    futures_month_from_symbol,
    parse_date,
    parse_money,
    parse_tick,
)


class TestParseTick:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("511-0", 5.11),        # whole cents
            ("511-2", 5.1125),      # quarter cent
            ("511-4", 5.115),       # half cent
            ("511-6", 5.1175),      # three quarter cent
            ("1259-6", 12.5975),    # four-digit soybean quote
            ("534-2", 5.3425),      # NEW Co-op format
        ],
    )
    def test_eighths_notation(self, raw, expected):
        assert parse_tick(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw,expected",
        [("-3-0", -0.03), ("-6-2", -0.0625), ("-2-2", -0.0225)],
    )
    def test_negative_change_ticks(self, raw, expected):
        assert parse_tick(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw,expected",
        [("+3-6", 0.0375), ("+2-0", 0.02), ("3-6", 0.0375)],
    )
    def test_positive_change_ticks(self, raw, expected):
        """NEW Co-op prints gains as "+3-6". An earlier regex only allowed a
        leading "-", which silently dropped every up day."""
        assert parse_tick(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["0-0", "", "   ", None, "-", "n/a"])
    def test_missing_quotes_are_none(self, raw):
        assert parse_tick(raw) is None

    def test_dollar_string_is_not_divided_by_100(self):
        assert parse_tick("$4.78") == pytest.approx(4.78)

    def test_bare_number_is_treated_as_cents(self):
        assert parse_tick(511) == pytest.approx(5.11)


class TestParseMoney:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("$4.78", 4.78),
            ("4.78", 4.78),
            ("$12.600", 12.60),
            ("-0.34", -0.34),
            ("$-0.03", -0.03),   # AgriCharts writes the sign inside the dollar sign
            ("-$0.03", -0.03),   # and some pages write it outside
            ("$1,234.56", 1234.56),
        ],
    )
    def test_values(self, raw, expected):
        assert parse_money(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "-", "N/A"])
    def test_blanks(self, raw):
        assert parse_money(raw) is None


class TestClassifyGrain:
    @pytest.mark.parametrize("raw", ["ZC", "ZCU26", "Corn", "CORN BIDS", "corn"])
    def test_corn(self, raw):
        assert classify_grain(raw) == CORN

    @pytest.mark.parametrize("raw", ["ZS", "ZSX26", "Soybeans", "SOYBEANS BIDS"])
    def test_soybeans(self, raw):
        assert classify_grain(raw) == SOYBEANS

    @pytest.mark.parametrize(
        "raw",
        [
            "Soybean Meal",        # a different commodity that contains "soybean"
            "Soybean Oil",
            "High Moisture Corn",  # not the cash commodity the map is about
            "Corn Silage",
            "Wheat",
            "Oats",
            "",
            None,
        ],
    )
    def test_rejects_everything_else(self, raw):
        assert classify_grain(raw) is None

    def test_first_usable_hint_wins(self):
        # sym_root is checked before the free-text name.
        assert classify_grain("ZC", "Some Local Product Name") == CORN

    def test_falls_through_to_later_hints(self):
        assert classify_grain(None, "", "Soybeans") == SOYBEANS


class TestDeliveryLabel:
    def test_two_month_window(self):
        assert format_delivery_label("2026-10-01", "2026-11-30") == "Oct/Nov 2026"

    def test_single_month(self):
        assert format_delivery_label("2026-08-01", "2026-08-31") == "Aug 2026"

    def test_partial_month_keeps_its_days(self):
        # Was "Sep 2026" until Cargill turned up quoting first-half, whole-month
        # and second-half November as three contracts - collapsing them all to
        # "Nov 2026" put three identical-looking rows in the popup.
        assert format_delivery_label("2026-09-01", "2026-09-10") == "Sep 1-10 2026"

    def test_half_month_windows_stay_distinct(self):
        first = format_delivery_label("2026-11-01", "2026-11-15")
        whole = format_delivery_label("2026-11-01", "2026-11-30")
        second = format_delivery_label("2026-11-16", "2026-11-30")
        assert first == "Nov 1-15 2026"
        assert whole == "Nov 2026"
        assert second == "Nov 16-30 2026"
        assert len({first, whole, second}) == 3

    def test_february_is_a_whole_month_in_a_leap_year(self):
        assert format_delivery_label("2028-02-01", "2028-02-29") == "Feb 2028"
        assert format_delivery_label("2026-02-01", "2026-02-28") == "Feb 2026"

    def test_crossing_the_year_boundary(self):
        assert format_delivery_label("2026-12-01", "2027-01-31") == "Dec 2026-Jan 2027"

    def test_no_start_date(self):
        assert format_delivery_label(None, "2026-11-30") is None


class TestMisc:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-08-31 23:59:59", "2026-08-31"),
            ("08/01/2026", "2026-08-01"),
            ("20260801", "2026-08-01"),
            ("2026-08-01", "2026-08-01"),
        ],
    )
    def test_parse_date(self, raw, expected):
        assert parse_date(raw) == expected

    def test_parse_date_rejects_junk(self):
        assert parse_date("not a date") is None

    @pytest.mark.parametrize(
        "raw,expected", [("ZCU26", "CU26"), ("ZSX26", "SX26"), ("CZ26", "CZ26")]
    )
    def test_futures_month(self, raw, expected):
        assert futures_month_from_symbol(raw) == expected


class TestTradeCodeCommodities:
    """Scoular labels commodities with trade codes, not names.

    The codes must match exactly. The same feed carries HRWW, HWW, DNSW, SWW,
    SOR, BLY and Winter Canola, and a substring match would sweep those in as
    corn or beans.
    """

    def test_corn_codes(self):
        for code in ("YC", "yc", "#2YC", "US#2YC"):
            assert classify_grain(code) == "corn"

    def test_soybean_codes(self):
        for code in ("YSB", "ysb", "YSB Mini", "#1YSB"):
            assert classify_grain(code) == "soybeans"

    def test_other_commodities_in_the_same_feed_are_rejected(self):
        for code in ("HRWW", "HWW", "DNSW", "SWW", "SOR", "Milo", "BLY",
                     "Winter Canola"):
            assert classify_grain(code) is None, code

    def test_named_specialty_soybeans_still_classify(self):
        assert classify_grain("IP Soybeans") == "soybeans"

    def test_derivatives_still_rejected(self):
        assert classify_grain("Soybean Meal") is None
        assert classify_grain("Corn Oil") is None
