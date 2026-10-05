from datetime import date, datetime, timezone

import httpx

from check_pages import propose_update
from tracker.extract import FIELDS, check
from tracker.models import Confidence, Race, Status
from tracker.propose import build_proposal, marker, pr_body, pr_title
from tracker.watch import Page, StateStore

TODAY = date(2026, 10, 3)
HOME = "https://example.com/test-marathon"
BALLOT = "https://example.com/test-marathon/ballot"
PAGES = {
    HOME: "Test Marathon\nRace day: Sunday 25 April 2027",
    BALLOT: "The ballot closes at midnight on 5 May 2026.\nResults by email in July.\nEntry costs £79.",
}


def raw(**fields):
    empty = {name: {"value": None, "quote": None} for name in FIELDS}
    return {**empty, **{k: {"value": v[0], "quote": v[1]} for k, v in fields.items()}}


def test_proposal_lists_changes_sources_and_sets_last_verified(confirmed_race):
    race = Race.model_validate(confirmed_race)
    extraction = check(race, PAGES, raw(
        race_date=("2027-04-25", "Race day: Sunday 25 April 2027"),  # unchanged
        ballot_closes=("2026-05-05T23:59", "The ballot closes at midnight on 5 May 2026."),
        price_gbp=("79", "Entry costs £79."),
    ))
    proposal = build_proposal(extraction, PAGES, TODAY)
    assert [c.name for c in proposal.changes] == ["ballot_closes", "price_gbp"]
    assert proposal.updates == {
        "ballot_closes": datetime(2026, 5, 5, 23, 59), "price_gbp": 79.0, "last_verified": TODAY,
    }
    assert proposal.sources == {"ballot_closes": BALLOT, "price_gbp": BALLOT}
    assert pr_title(proposal) == "Update Test Marathon: ballot_closes, price_gbp"

    body = pr_body(proposal, {BALLOT: ["-old", "+new"]}, "test-model")
    assert marker(race.id) in body
    assert "| `ballot_closes` | 2026-05-02T12:00 | **2026-05-05T23:59** |" in body
    assert f"([page]({BALLOT}))" in body
    assert "```diff\n-old\n+new\n```" in body


def test_estimated_race_becomes_confirmed_with_a_source(confirmed_race):
    race = Race.model_validate({**confirmed_race, "confidence": "estimated", "source_url": None,
                                "last_verified": None, "race_date": None})
    extraction = check(race, PAGES, raw(race_date=("2027-04-25", "Race day: Sunday 25 April 2027")))
    proposal = build_proposal(extraction, PAGES, TODAY)
    assert proposal.updates["confidence"] == Confidence.confirmed
    assert proposal.updates["source_url"] == HOME


def test_rejected_and_date_only_values_are_left_for_a_person(confirmed_race):
    race = Race.model_validate(confirmed_race)
    extraction = check(race, PAGES, raw(
        ballot_results=("2026-07-01", "made up quote"),
        ballot_opens=("2026-04-20", "Race day: Sunday 25 April 2027"),
    ))
    proposal = build_proposal(extraction, PAGES, TODAY)
    assert not proposal.has_changes
    assert any("`ballot_results`" in n and "rejected" in n for n in proposal.for_a_person)
    assert any("`ballot_opens`" in n and "no time" in n for n in proposal.for_a_person)


class FakeLLM:
    model = "fake"

    def __init__(self, answer):
        self.answer = answer

    def generate_json(self, system, prompt, schema):
        return self.answer


class FakePulls:
    def __init__(self, text):
        self.text, self.proposed = text, []

    def read_data_file(self):
        return self.text

    def propose(self, race_id, new_text, title, body):
        self.proposed.append((race_id, new_text, title, body))
        return "https://github.com/o/r/pull/9"


YAML = """races:
  - id: test-marathon-2027
    name: Test Marathon
    distance: marathon
    location: London
    race_date: 2027-04-25
    entry_type: [ballot, charity]
    ballot_opens: 2026-04-27T10:00
    ballot_closes: 2026-05-02T12:00
    ballot_results: 2026-07-01
    official_url: https://example.com/test-marathon
    source_url: https://example.com/test-marathon/ballot
    status: announced
    last_verified: 2026-04-20
    confidence: confirmed
"""


def stored(tmp_path, race):
    store = StateStore(tmp_path)
    for url, text in PAGES.items():
        store.write_text(Page(url, [race]), text)
    return store


NOW = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


def test_propose_update_opens_a_pull_request_with_a_valid_edit(tmp_path, confirmed_race):
    race = Race.model_validate(confirmed_race)
    pulls = FakePulls(YAML)
    llm = FakeLLM(raw(status=("ballot_closed", "The ballot closes at midnight on 5 May 2026.")))
    outcome = propose_update(race, stored(tmp_path, race), {BALLOT: ["+x"]}, llm, pulls, NOW)
    assert outcome.kind == "pr" and outcome.url == "https://github.com/o/r/pull/9"
    race_id, new_text, title, _ = pulls.proposed[0]
    assert race_id == race.id and title == "Update Test Marathon: status"
    assert "    status: ballot_closed\n    last_verified: 2026-10-03\n" in new_text


def test_propose_update_notes_when_nothing_changed(tmp_path, confirmed_race):
    race = Race.model_validate(confirmed_race)
    pulls = FakePulls(YAML)
    llm = FakeLLM(raw(status=("announced", "Test Marathon")))
    outcome = propose_update(race, stored(tmp_path, race), {}, llm, pulls, NOW)
    assert outcome.kind == "cleared" and "no changes to the race details found" in outcome.note
    assert pulls.proposed == []


def test_propose_update_survives_llm_failures(tmp_path, confirmed_race):
    from tracker.llm import LLMError

    class Broken(FakeLLM):
        def generate_json(self, system, prompt, schema):
            raise LLMError("daily quota used up")

    race = Race.model_validate(confirmed_race)
    outcome = propose_update(race, stored(tmp_path, race), {}, Broken(None), FakePulls(YAML), NOW)
    assert outcome.kind == "review" and "couldn't run (daily quota used up)" in outcome.note


def test_refused_pull_request_falls_back_to_an_issue_note(tmp_path, confirmed_race):
    class Refused(FakePulls):
        def propose(self, race_id, new_text, title, body):
            request = httpx.Request("POST", "https://api.github.com/repos/o/r/pulls")
            raise httpx.HTTPStatusError("403 Forbidden", request=request, response=httpx.Response(403, request=request))

    race = Race.model_validate(confirmed_race)
    llm = FakeLLM(raw(status=("ballot_closed", "The ballot closes at midnight on 5 May 2026.")))
    outcome = propose_update(race, stored(tmp_path, race), {}, llm, Refused(YAML), NOW)
    assert outcome.kind == "review"
    note = outcome.note
    assert "pull request couldn't be opened" in note and "`status`: announced → ballot_closed" in note


def test_things_for_a_person_keep_the_change_open_for_review(tmp_path, confirmed_race):
    race = Race.model_validate(confirmed_race)
    llm = FakeLLM(raw(ballot_opens=("2026-04-20", "Race day: Sunday 25 April 2027")))  # date with no time
    outcome = propose_update(race, stored(tmp_path, race), {}, llm, FakePulls(YAML), NOW)
    assert outcome.kind == "review" and "`ballot_opens`" in outcome.note
