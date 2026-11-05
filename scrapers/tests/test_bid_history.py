"""Daily bid history: one closing quote per pin, grain and window per day."""

import bid_history


def _data(generated_at, as_of, cash, stale=False):
    return {"generated_at": generated_at, "locations": {"pin-a": {
        "name": "Elevator A", "source": "s", "as_of": as_of, "stale": stale,
        "bids": [{"grain": "corn", "delivery_start": "2026-11-01",
                  "delivery_end": "2026-11-30", "delivery_label": "Nov 2026",
                  "futures_month": "CZ26", "futures": 4.4, "basis": round(cash - 4.4, 4),
                  "cash": cash}]}}}


def _rows(db):
    return db.execute("SELECT day, cash, basis FROM quotes ORDER BY day").fetchall()


def test_later_scrape_same_day_is_the_close(tmp_path):
    db = bid_history.connect(tmp_path / "h.sqlite")
    bid_history.record(db, _data("2026-11-05T15:00:00Z", "2026-11-05T14:55:00Z", 4.00))
    bid_history.record(db, _data("2026-11-05T21:00:00Z", "2026-11-05T20:55:00Z", 4.05))
    assert _rows(db) == [("2026-11-05", 4.05, -0.35)]


def test_an_older_scrape_never_overwrites_a_newer_one(tmp_path):
    # The backfill can run after the live task has already recorded today.
    db = bid_history.connect(tmp_path / "h.sqlite")
    bid_history.record(db, _data("2026-11-05T21:00:00Z", "2026-11-05T20:55:00Z", 4.05))
    bid_history.record(db, _data("2026-11-05T15:00:00Z", "2026-11-05T14:55:00Z", 4.00))
    assert _rows(db) == [("2026-11-05", 4.05, -0.35)]


def test_day_is_the_quote_date_in_central_time(tmp_path):
    # Heartland's 1:15 PM close, scraped the next morning, belongs to its own
    # day; and 8 PM Central is still that day although UTC has rolled over.
    db = bid_history.connect(tmp_path / "h.sqlite")
    bid_history.record(db, _data("2026-11-06T14:00:00Z", "2026-11-05T18:15:00Z", 4.00))
    bid_history.record(db, _data("2026-11-07T02:00:00Z", "2026-11-07T02:00:00Z", 4.10))
    assert [r[0] for r in _rows(db)] == ["2026-11-05", "2026-11-06"]


def test_weekend_reruns_do_not_add_days(tmp_path):
    db = bid_history.connect(tmp_path / "h.sqlite")
    for scraped in ("2026-11-06T22:00:00Z", "2026-11-07T15:00:00Z", "2026-11-08T15:00:00Z"):
        bid_history.record(db, _data(scraped, "2026-11-06T20:00:00Z", 4.00))
    assert [r[0] for r in _rows(db)] == ["2026-11-06"]


def test_carried_forward_pins_are_not_recorded(tmp_path):
    db = bid_history.connect(tmp_path / "h.sqlite")
    assert bid_history.record(db, _data("2026-11-05T21:00:00Z", "2026-11-02T20:00:00Z",
                                        4.00, stale=True)) == 0
    assert _rows(db) == []
