"""Tests for pin-to-source matching.

Matching is the part of this project where a bug is *quiet*: a wrong match does
not throw, it just shows a farmer the wrong elevator's bid. These tests pin down
the cases that have actually gone wrong.
"""

import pytest

import match_locations as m


class TestNameScore:
    def test_state_codes_carry_no_signal(self):
        """POET Glenville MN was once matched to Preston, MN purely because both
        end in "MN". Any two facilities in a state shared that token, which was
        enough to clear the publish threshold."""
        assert m.name_score("POET Glenville MN Ethanol", "Preston, MN") == 0.0
        assert m.name_score("ADM Carthage MO", "Charleston, MO") == 0.0

    def test_the_real_town_still_scores_full(self):
        assert m.name_score("POET Glenville MN Ethanol", "Glenville, MN") == 1.0
        assert m.name_score("POET Ashton Ethanol", "Ashton, IA") == 1.0
        assert m.name_score("Flint Hills/Poet ethanol Menlo", "Menlo, IA") == 1.0

    def test_town_named_after_a_state_is_not_stopworded(self):
        """Only two-letter abbreviations are excluded. Nevada, Iowa is a real
        delivery point, so full state names must stay matchable."""
        assert m.name_score("Key Coop Nevada IA", "Nevada, IA") == 1.0

    def test_verbose_pin_name_against_terse_source_name(self):
        # Containment, not Jaccard: pin names carry the company, sources do not.
        assert m.name_score("gold eagle coop hutchins", "Hutchins") == 1.0

    def test_unrelated_names_score_zero(self):
        assert m.name_score("Heartland Coop Alleman", "Bettendorf") == 0.0


class TestMatchPin:
    @staticmethod
    def pin(name, lat=42.0, lng=-93.0):
        return {"name": name, "lat": lat, "lng": lng, "company": "", "host": ""}

    def test_geography_wins_when_coordinates_are_published(self):
        cands = [
            {"source_location_id": "1", "name": "Somewhere Else",
             "latitude": 42.001, "longitude": -93.001},
            {"source_location_id": "2", "name": "Far Away",
             "latitude": 45.0, "longitude": -96.0},
        ]
        best, conf, method = m.match_pin(self.pin("Anything"), cands)
        assert best["source_location_id"] == "1"
        assert method.startswith("geo")
        assert conf > 0.9

    def test_no_match_when_nothing_is_close_or_similar(self):
        cands = [{"source_location_id": "9", "name": "Totally Unrelated",
                  "latitude": 48.0, "longitude": -100.0}]
        best, conf, method = m.match_pin(self.pin("Heartland Coop Alleman"), cands)
        assert best is None
        assert conf == 0.0

    def test_duplicate_names_are_flagged_ambiguous(self):
        """Two ADM facilities in one town cannot be told apart by name, so the
        match is downgraded rather than guessed at.

        Both candidates reduce to {quincy} here - "elevator" and "terminal" are
        both stopwords - so they tie exactly, which is the condition that has to
        be caught.
        """
        cands = [
            {"source_location_id": "a", "name": "Quincy, IL (Elevator)"},
            {"source_location_id": "b", "name": "Quincy, IL (Terminal)"},
        ]
        best, conf, method = m.match_pin(self.pin("ADM Quincy IL"), cands)
        assert method == "name-ambiguous"
        assert conf < 0.35, "an ambiguous match must fall below the publish bar"

    def test_strong_tie_is_broken_by_character_similarity(self):
        """"Creston 1" and "Creston 2" both reduce to {creston}; the digit is
        only visible at character level."""
        cands = [
            {"source_location_id": "a", "name": "Creston 1"},
            {"source_location_id": "b", "name": "Creston 2"},
        ]
        best, conf, method = m.match_pin(self.pin("New Coop Creston 1"), cands)
        assert method == "name-tiebreak"
        assert best["name"] == "Creston 1"
        assert conf >= 0.35, "a resolved tie should publish"

        best, _, _ = m.match_pin(self.pin("New Coop Creston 2"), cands)
        assert best["name"] == "Creston 2"

    def test_suffixed_duplicate_resolves_to_the_plain_name(self):
        cands = [
            {"source_location_id": "a", "name": "EARLHAM"},
            {"source_location_id": "b", "name": "EARLHAM FEED MILL"},
        ]
        best, _, method = m.match_pin(self.pin("Heartland Coop Earlham"), cands)
        assert method == "name-tiebreak"
        assert best["name"] == "EARLHAM"

    def test_weak_ties_are_never_broken(self):
        """The regression this guards, using the case that exposed it.

        Landus sold Davis City, so it is simply not in their location list any
        more. The map pin still says "Landus Davis City", leaving only wrong
        candidates that tie weakly - and breaking that tie published Lake City's
        bid under Davis City's pin.

        The general lesson: a tie between weak candidates usually means the
        right answer is absent, not that it is one of the two.
        """
        cands = [
            {"source_location_id": "a", "name": "Lake City"},
            {"source_location_id": "b", "name": "Rockwell City"},
        ]
        _, conf, method = m.match_pin(self.pin("Landus Davis City"), cands)
        assert method == "name-ambiguous"
        assert conf < 0.35, "a weak tie must not be published"

    def test_a_distinguishing_word_resolves_the_tie(self):
        """When one candidate carries a word the other does not, it is no longer
        ambiguous - "barge dock" survives token filtering where "elevator"
        does not."""
        cands = [
            {"source_location_id": "a", "name": "Quincy, IL (Barge Dock)"},
            {"source_location_id": "b", "name": "Quincy, IL (Elevator)"},
        ]
        _, _, method = m.match_pin(self.pin("ADM Quincy IL"), cands)
        assert method == "name"

    def test_empty_candidate_list(self):
        best, conf, method = m.match_pin(self.pin("Anything"), [])
        assert best is None and method == "no-candidates"


class TestSlugStability:
    def test_slug_matches_what_the_map_computes(self):
        """The front end derives the same slug from the pin name at runtime, so
        these two implementations must not drift."""
        assert m.slugify("Gold-Eagle Coop Clarion") == "gold-eagle-coop-clarion"
        assert m.slugify("CVA 81-20 McLean NE") == "cva-81-20-mclean-ne"
        assert m.slugify("Heartland Coop Avon (Carlisle)") == "heartland-coop-avon-carlisle"
