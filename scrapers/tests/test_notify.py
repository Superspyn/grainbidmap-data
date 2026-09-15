"""The text cap. An explicit 0 means off; it used to mean six."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "dev"))

import jd_notify  # noqa: E402


def _wire(monkeypatch, cfg, sent):
    monkeypatch.setattr(jd_notify, "load_config", lambda: cfg)
    monkeypatch.setattr(jd_notify, "_state", lambda: {"sent": []})
    monkeypatch.setattr(jd_notify, "_save_state", lambda state: None)
    monkeypatch.setattr(jd_notify, "in_quiet_hours", lambda cfg, now: False)
    monkeypatch.setattr(jd_notify, "send", lambda cfg, body, dry: sent.append(body) or True)


IDLING = {"name": "Red 1Ton", "engine": "idling", "idle_min": 12, "minutes": 12,
          "start": "2026-09-12T18:00:00Z", "y": 43.1, "x": -93.8}


def test_cap_of_zero_sends_nothing(monkeypatch):
    sent = []
    _wire(monkeypatch, {"provider": "email", "to": ["x@vtext.com"], "max_per_hour": 0}, sent)
    assert jd_notify.notify_stops([IDLING], [], fields=[]) == 0
    assert sent == []


def test_missing_cap_uses_the_default(monkeypatch):
    sent = []
    _wire(monkeypatch, {"provider": "email", "to": ["x@vtext.com"]}, sent)
    assert jd_notify.notify_stops([IDLING], [], fields=[]) == 1
    assert len(sent) == 1


def test_parked_and_unknown_stops_are_never_texted(monkeypatch):
    sent = []
    _wire(monkeypatch, {"provider": "email", "to": ["x@vtext.com"]}, sent)
    stops = [dict(IDLING, engine="parked"), dict(IDLING, engine="unknown", idle_min=None)]
    assert jd_notify.notify_stops(stops, [], fields=[]) == 0
    assert sent == []
