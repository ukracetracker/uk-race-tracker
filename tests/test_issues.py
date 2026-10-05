import json
from datetime import datetime, timezone

import httpx

from tracker.issues import CHANGE_LABEL, GitHubIssues, change_report, issue_title, marker
from tracker.models import Race
from tracker.watch import CheckResult, Page

NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)


def changed(confirmed_race) -> CheckResult:
    race = Race.model_validate(confirmed_race)
    return CheckResult(Page(str(race.official_url), [race]), "changed", diff=["-Ballot TBC", "+Ballot opens 1 May"])


def fake_github(existing_issues):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.method == "GET":
            return httpx.Response(200, json=existing_issues)
        if request.url.path.endswith("/labels"):
            return httpx.Response(422, json={})
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/7"})

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubIssues("o/r", "token", client=client), calls


def two_pages_changed(confirmed_race) -> tuple[Race, list[CheckResult]]:
    race = Race.model_validate(confirmed_race)
    other = CheckResult(Page(str(race.source_url), [race]), "changed", diff=["+Register"])
    return race, [changed(confirmed_race), other]


def test_one_report_covers_every_changed_page_of_a_race(confirmed_race):
    race, results = two_pages_changed(confirmed_race)
    body = change_report(race, results, NOW)
    assert marker("test-marathon-2027") in body
    assert "Pages for **Test Marathon** (`test-marathon-2027`) changed on Fri 02 Oct 2026." in body
    assert "**https://example.com/test-marathon**" in body and "**https://example.com/test-marathon/ballot**" in body
    assert "+Ballot opens 1 May" in body and "+Register" in body
    assert "- [ ] Close this issue" in body
    assert issue_title(race) == "Page changed: Test Marathon"


def test_opens_one_issue_per_race_with_label(confirmed_race):
    race, results = two_pages_changed(confirmed_race)
    issues, calls = fake_github([])
    assert issues.report_change(race, results, NOW) == "https://github.com/o/r/issues/7"
    posts = [c for c in calls if c[:2] == ("POST", "/repos/o/r/issues")]
    assert len(posts) == 1 and posts[0][2]["labels"] == [CHANGE_LABEL]
    assert not any(m == "PATCH" for m, _, _ in calls)


def test_cleared_change_is_opened_already_closed(confirmed_race):
    race, results = two_pages_changed(confirmed_race)
    issues, calls = fake_github([])
    issues.report_change(race, results, NOW, note="**LLM check:** no changes.\n\n", cleared=True)
    post = next(c for c in calls if c[:2] == ("POST", "/repos/o/r/issues"))
    assert post[2]["title"] == "Page changed, race details unchanged: Test Marathon"
    assert "closed automatically" in post[2]["body"] and "- [ ]" not in post[2]["body"]
    assert calls[-1] == ("PATCH", "/repos/o/r/issues/7", {"state": "closed", "state_reason": "completed"})


def test_comments_on_the_race_s_open_issue(confirmed_race):
    race, results = two_pages_changed(confirmed_race)
    issues, calls = fake_github([{"number": 3, "body": "older\n" + marker("test-marathon-2027")}])
    issues.report_change(race, results, NOW, cleared=True)
    assert calls[-1][:2] == ("POST", "/repos/o/r/issues/3/comments")
    assert not any(m == "PATCH" for m, _, _ in calls)  # an open issue a person is handling stays open
    assert not any(path.endswith("/labels") for _, path, _ in calls)


def failing(confirmed_race, days=7) -> CheckResult:
    race = Race.model_validate(confirmed_race)
    return CheckResult(Page(str(race.official_url), [race]), "failed", "HTTP 403", failing_days=days)


def test_opens_one_unreachable_issue_per_page(confirmed_race):
    from tracker.issues import UNREACHABLE_LABEL, unreachable_marker

    issues, calls = fake_github([])
    assert issues.report_unreachable(failing(confirmed_race)) == "https://github.com/o/r/issues/7"
    method, path, payload = calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues")
    assert payload["title"] == "Page unreachable for 7 days: Test Marathon"
    assert payload["labels"] == [UNREACHABLE_LABEL]
    assert "HTTP 403" in payload["body"] and "monitoring: manual" in payload["body"]

    already = [{"number": 4, "body": unreachable_marker("https://example.com/test-marathon")}]
    issues, calls = fake_github(already)
    assert issues.report_unreachable(failing(confirmed_race, days=8)) is None
    assert [c[0] for c in calls] == ["GET"]  # no new issue, no daily comment


def test_closes_unreachable_issue_when_page_recovers(confirmed_race):
    from tracker.issues import unreachable_marker

    existing = [{"number": 4, "html_url": "https://github.com/o/r/issues/4",
                 "body": unreachable_marker("https://example.com/test-marathon")}]
    issues, calls = fake_github(existing)
    race = Race.model_validate(confirmed_race)
    ok = CheckResult(Page(str(race.official_url), [race]), "unchanged", recovered=True)
    assert issues.close_unreachable(ok) == "https://github.com/o/r/issues/4"
    assert calls[-2][:2] == ("POST", "/repos/o/r/issues/4/comments")
    assert calls[-1][:2] == ("PATCH", "/repos/o/r/issues/4") and calls[-1][2]["state"] == "closed"
