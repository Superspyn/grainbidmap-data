"""What is planted in a field, from its operations.

The rule under test: only a SEEDING pass says what went in, the latest one
wins, and if this season has none yet the previous season's crop is given
with its year so the page can label it. The harvest-pass trap is real: one
field read BARLEY on its 2026 harvest pass in a year it was seeded to corn.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dev"))

import jd_fleet  # noqa: E402


def test_crop_words():
    assert jd_fleet.crop_word("CORN_WET") == "corn"
    assert jd_fleet.crop_word("CORN_SILAGE") == "corn"
    assert jd_fleet.crop_word("SOYBEANS") == "soybeans"
    assert jd_fleet.crop_word("WHEAT_HARD_RED_WINTER") == "wheat hard red winter"
    assert jd_fleet.crop_word("") is None
    assert jd_fleet.crop_word(None) is None


def _ops_by_season(table):
    """Stand-in for api_all keyed on the cropSeason in the URL."""
    def fake(token, url):
        season = int(url.rsplit("cropSeason=", 1)[1])
        return table.get(season, [])
    return fake


def test_seeding_pass_names_the_crop_and_harvest_does_not(monkeypatch):
    monkeypatch.setattr(jd_fleet, "api_all", _ops_by_season({2026: [
        {"fieldOperationType": "tillage", "cropName": None, "startDate": "2026-03-31T13:12:25Z"},
        {"fieldOperationType": "seeding", "cropName": "CORN_WET", "startDate": "2026-04-16T22:21:15Z"},
        {"fieldOperationType": "harvest", "cropName": "BARLEY", "startDate": "2026-08-25T13:17:52Z"},
    ]}))
    got = jd_fleet.field_crop("t", "5294", "f1", season=2026)
    assert got["crop"] == "corn"
    assert got["crop_code"] == "CORN_WET"
    assert got["crop_season"] == 2026
    assert got["planted"] == "2026-04-16T22:21:15Z"


def test_latest_seeding_pass_wins(monkeypatch):
    """A replant is the crop that is actually in the ground."""
    monkeypatch.setattr(jd_fleet, "api_all", _ops_by_season({2026: [
        {"fieldOperationType": "seeding", "cropName": "CORN_WET", "startDate": "2026-04-20T00:00:00Z"},
        {"fieldOperationType": "seeding", "cropName": "SOYBEANS", "startDate": "2026-06-02T00:00:00Z"},
    ]}))
    assert jd_fleet.field_crop("t", "o", "f", season=2026)["crop"] == "soybeans"


def test_before_planting_falls_back_to_last_season_and_says_so(monkeypatch):
    monkeypatch.setattr(jd_fleet, "api_all", _ops_by_season({
        2027: [{"fieldOperationType": "tillage", "cropName": None, "startDate": "2027-03-01T00:00:00Z"}],
        2026: [{"fieldOperationType": "seeding", "cropName": "SOYBEANS", "startDate": "2026-05-01T00:00:00Z"}],
    }))
    got = jd_fleet.field_crop("t", "o", "f", season=2027)
    assert got["crop"] == "soybeans"
    assert got["crop_season"] == 2026


def test_no_seeding_pass_at_all_is_empty_not_a_guess(monkeypatch):
    monkeypatch.setattr(jd_fleet, "api_all", _ops_by_season({}))
    assert jd_fleet.field_crop("t", "o", "f", season=2026) == {}
