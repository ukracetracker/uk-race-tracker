"""GitHub issues for the daily page check: changed pages and pages that keep failing."""

from __future__ import annotations

from datetime import datetime

import httpx

from tracker.models import Race
from tracker.watch import CheckResult

API = "https://api.github.com"
CHANGE_LABEL = "page-change"
UNREACHABLE_LABEL = "page-unreachable"
LABELS = {
    CHANGE_LABEL: ("d97706", "An official race page changed"),
    UNREACHABLE_LABEL: ("b91c1c", "An official race page has failed to load for days"),
}


def marker(race_id: str) -> str:
    """Hidden tag that ties a change issue to one race, so repeat changes become comments."""
    return f"<!-- race-watch: {race_id} -->"


def unreachable_marker(url: str) -> str:
    return f"<!-- page-unreachable: {url} -->"


def issue_title(race: Race, cleared: bool = False) -> str:
    if cleared:
        return f"Page changed, race details unchanged: {race.name}"
    return f"Page changed: {race.name}"


def change_report(race: Race, results: list[CheckResult], now: datetime, note: str = "", cleared: bool = False) -> str:
    diffs = "".join(
        f"\n**{r.page.url}** (lines starting `-` were removed, `+` were added)\n\n```diff\n"
        + ("\n".join(r.diff) or "(no line-level differences)")
        + "\n```\n"
        for r in results
    )
    if cleared:
        todo = (
            "The LLM read the race's pages and found no changes to its dates, entry details or status, "
            "so this was closed automatically. Reopen it if you spot something it missed."
        )
    else:
        todo = """**To do**
- [ ] Open the page and check whether any dates, prices or entry details changed
- [ ] If they did, update `races.yaml` (including `status`, `confidence`, `source_url` and `last_verified`)
- [ ] Close this issue"""
    return f"""{marker(race.id)}
{"A page" if len(results) == 1 else "Pages"} for **{race.name}** (`{race.id}`) changed on {now:%a %d %b %Y}.
{diffs}
{note}{todo}

Menus, footers and scripts are ignored, so this is the pages' visible text only.
"""


def unreachable_report(result: CheckResult) -> str:
    page = result.page
    races = "\n".join(f"- {race.name} (`{race.id}`)" for race in page.races)
    return f"""{unreachable_marker(page.url)}
The daily check hasn't been able to read this page for {result.failing_days} days: {page.url}

Latest error: **{result.detail}**

**Races using this page**
{races}

While this is open, changes to the page are **not** being picked up.

**What to do**
- [ ] Open the page in a browser. If it works there, the site is probably blocking automated checks (e.g. HTTP 403 from GitHub's servers).
  Set `monitoring: manual` for these races in `races.yaml` and check them by hand.
- [ ] If the address has moved, update `official_url` instead.
- [ ] Close this issue. It also closes itself if the page starts loading again.
"""


class GitHubIssues:
    def __init__(self, repo: str, token: str, client: httpx.Client | None = None) -> None:
        self.repo = repo
        self._client = client or httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        self._labels_ready: set[str] = set()

    def report_change(
        self, race: Race, results: list[CheckResult], now: datetime, note: str = "", cleared: bool = False
    ) -> str:
        """One issue per race. Comment on its open issue if there is one, else open a new one (closed
        straight away if the LLM cleared the change). Returns the issue or comment URL."""
        body = change_report(race, results, now, note, cleared)
        existing = self._find_open_issue(CHANGE_LABEL, marker(race.id))
        if existing:
            return self._comment(existing["number"], body)
        url = self._open(issue_title(race, cleared), body, CHANGE_LABEL)
        if cleared:
            number = url.rstrip("/").rsplit("/", 1)[-1]
            response = self._client.patch(
                f"/repos/{self.repo}/issues/{number}", json={"state": "closed", "state_reason": "completed"}
            )
            response.raise_for_status()
        return url

    def report_unreachable(self, result: CheckResult) -> str | None:
        """Open one issue per failing page; returns its URL, or None if one is already open."""
        if self._find_open_issue(UNREACHABLE_LABEL, unreachable_marker(result.page.url)):
            return None
        title = f"Page unreachable for {result.failing_days} days: {result.page.label}"
        return self._open(title, unreachable_report(result), UNREACHABLE_LABEL)

    def close_unreachable(self, result: CheckResult) -> str | None:
        """Close the page's open unreachable issue, if any, now that it loads again."""
        existing = self._find_open_issue(UNREACHABLE_LABEL, unreachable_marker(result.page.url))
        if not existing:
            return None
        self._comment(existing["number"], "The page loaded successfully again, so the daily check is back on. Closing.")
        response = self._client.patch(
            f"/repos/{self.repo}/issues/{existing['number']}", json={"state": "closed", "state_reason": "completed"}
        )
        response.raise_for_status()
        return existing["html_url"]

    def _open(self, title: str, body: str, label: str) -> str:
        self._ensure_label(label)
        response = self._client.post(f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": [label]})
        response.raise_for_status()
        return response.json()["html_url"]

    def _comment(self, number: int, body: str) -> str:
        response = self._client.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})
        response.raise_for_status()
        return response.json()["html_url"]

    def _find_open_issue(self, label: str, tag: str) -> dict | None:
        response = self._client.get(
            f"/repos/{self.repo}/issues", params={"state": "open", "labels": label, "per_page": 100}
        )
        response.raise_for_status()
        for issue in response.json():
            if tag in (issue.get("body") or ""):
                return issue
        return None

    def _ensure_label(self, label: str) -> None:
        if label in self._labels_ready:
            return
        colour, description = LABELS[label]
        response = self._client.post(
            f"/repos/{self.repo}/labels", json={"name": label, "color": colour, "description": description}
        )
        if response.status_code not in (201, 422):  # 422: label already exists
            response.raise_for_status()
        self._labels_ready.add(label)
