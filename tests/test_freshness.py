from datetime import date

import pytest

from tracker.freshness import Freshness, race_freshness, states_by_url
from tracker.models import Race
from tracker.watch import PageState

TODAY = date(2026, 10, 10)
HOME = "https://example.com/test-marathon"
BALLOT = "https://example.com/test-marathon/ballot"


def state(url, last_ok="2026-10-10T06:00:00+00:00", last_changed=None, failures=0):
    return PageState(url=url, last_ok=last_ok, last_changed=last_changed, consecutive_failures=failures)


@pytest.fixture
def race(confirmed_race) -> Race:
    return Race.model_validate({**confirmed_race, "last_verified": "2026-10-02"})


def test_unchanged_pages_read_today(race):
    states = {HOME: state(HOME), BALLOT: state(BALLOT, last_changed="2026-10-01T06:00:00+00:00")}
    f = race_freshness(race, states, TODAY)
    assert f.status == Freshness.unchanged and f.text == "page unchanged, checked 10 Oct"


def test_change_after_verification_is_flagged(race):
    states = {HOME: state(HOME), BALLOT: state(BALLOT, last_changed="2026-10-08T06:00:00+00:00")}
    f = race_freshness(race, states, TODAY)
    assert f.status == Freshness.changed and f.text == "page changed 08 Oct: being reviewed"


def test_change_on_the_verification_day_counts_as_verified(race):
    states = {HOME: state(HOME, last_changed="2026-10-02T06:00:00+00:00"), BALLOT: state(BALLOT)}
    assert race_freshness(race, states, TODAY).status == Freshness.unchanged


def test_pages_blocked_for_days_fall_back_to_by_hand(race):
    states = {HOME: state(HOME, last_ok="2026-10-02T06:00:00+00:00", failures=8), BALLOT: state(BALLOT)}
    f = race_freshness(race, states, TODAY)
    assert f.status == Freshness.by_hand and "checked by hand" in f.text
    never_loaded = {HOME: state(HOME, last_ok=None, failures=3), BALLOT: state(BALLOT)}
    assert race_freshness(race, never_loaded, TODAY).status == Freshness.by_hand


def test_manual_and_unknown(race, confirmed_race):
    manual = Race.model_validate({**confirmed_race, "monitoring": "manual"})
    assert race_freshness(manual, {}, TODAY).text == "checked by hand"
    assert race_freshness(race, {HOME: state(HOME)}, TODAY).status == Freshness.unknown  # no record for BALLOT


def test_states_are_looked_up_by_url():
    assert states_by_url({"key": state(HOME)}) == {HOME: state(HOME)}


def test_a_change_the_llm_cleared_counts_as_reviewed(race):
    states = {
        HOME: state(HOME, last_changed="2026-10-08T06:00:00+00:00"),
        BALLOT: PageState(url=BALLOT, last_ok="2026-10-10T06:00:00+00:00", last_changed="2026-10-08T06:00:00+00:00",
                          last_cleared="2026-10-08T06:01:00+00:00"),
    }
    assert race_freshness(race, states, TODAY).status == Freshness.unchanged
    states[HOME].last_changed = "2026-10-09T06:00:00+00:00"  # a newer change nobody has cleared
    assert race_freshness(race, states, TODAY).status == Freshness.changed
