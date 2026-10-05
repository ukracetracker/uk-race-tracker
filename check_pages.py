"""Check each race's official page for changes and report them as GitHub issues.

    python check_pages.py             # check pages, update state/, print what changed
    python check_pages.py --issues    # also open or update GitHub issues (needs GITHUB_TOKEN
                                      # and GITHUB_REPOSITORY, as set in GitHub Actions)

With --issues, a page that has failed for --alert-after-days days gets one
"page-unreachable" issue, which is closed automatically when the page loads
again. When a race's pages change, it gets one "page-change" issue with the
diffs. With GEMINI_API_KEY set, the LLM reads the race's pages first:
  - new values: a pull request proposes the races.yaml edit instead of an issue
  - nothing changed: the issue is opened already closed, as a record, and the
    change counts as reviewed on the site
  - anything else (LLM failed, values to check by hand): the issue stays open

    python check_pages.py --issues --propose-for cardiff-half-2027
                                      # run the LLM proposal for one race now,
                                      # even though its pages haven't changed
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tracker.extract import extract
from tracker.fetch import Fetcher
from tracker.issues import GitHubIssues
from tracker.llm import DEFAULT_GEMINI_MODEL, GeminiClient, JsonLLM, LLMError
from tracker.loader import DataError, load_races, parse_races
from tracker.models import Race
from tracker.propose import format_cell, build_proposal, pr_body, pr_title
from tracker.pulls import GitHubPulls
from tracker.watch import CheckResult, Page, StateStore, check_pages, race_urls, watched_pages
from tracker.yamledit import update_race

ROOT = Path(__file__).resolve().parent
DEFAULT_ALERT_AFTER_DAYS = 7


def summary(results: list[CheckResult], issue_links: dict[str, str]) -> str:
    counts = {outcome: sum(r.outcome == outcome for r in results) for outcome in ("changed", "unchanged", "baseline", "failed")}
    lines = [
        "## Race page check",
        "",
        f"{len(results)} pages: {counts['changed']} changed, {counts['unchanged']} unchanged, "
        f"{counts['baseline']} first seen, {counts['failed']} failed.",
        "",
        "| Page | Result |",
        "| --- | --- |",
    ]
    for r in results:
        detail = {"changed": "changed", "unchanged": "no change", "baseline": "first snapshot saved"}.get(r.outcome)
        if r.outcome == "failed":
            days = f"{r.failing_days} day{'s' if r.failing_days != 1 else ''}"
            detail = f"failed ({r.detail}; failing for {days})" if r.failing_days else f"failed ({r.detail})"
        link = issue_links.get(r.page.url)
        if link:
            detail += f" · [{'pull request' if '/pull/' in link else 'issue'}]({link})"
        lines.append(f"| [{r.page.label}]({r.page.url}) | {detail} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "races.yaml")
    parser.add_argument("--state", type=Path, default=ROOT / "state")
    parser.add_argument("--issues", action="store_true", help="open or update GitHub issues")
    parser.add_argument("--propose-for", metavar="RACE_ID", help="run the LLM proposal for this race now")
    parser.add_argument(
        "--alert-after-days",
        type=int,
        default=int(os.environ.get("ALERT_AFTER_DAYS") or DEFAULT_ALERT_AFTER_DAYS),
        help="open a page-unreachable issue once a page has failed for this many days (default 7)",
    )
    args = parser.parse_args(argv)

    issues = pulls = llm = None
    if args.issues:
        token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        if not token or not repo:
            print("--issues needs GITHUB_TOKEN and GITHUB_REPOSITORY", file=sys.stderr)
            return 2
        issues = GitHubIssues(repo, token)
        pulls = GitHubPulls(repo, token)
    if os.environ.get("GEMINI_API_KEY"):
        llm = GeminiClient(os.environ["GEMINI_API_KEY"], model=os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL)
    if args.propose_for and not (pulls and llm):
        print("--propose-for needs --issues and GEMINI_API_KEY", file=sys.stderr)
        return 2

    try:
        races = load_races(args.data).races
    except DataError as exc:
        print(exc, file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).replace(microsecond=0)
    store = StateStore(args.state)
    with Fetcher() as fetcher:
        results = check_pages(watched_pages(races), fetcher, store, now)

    issue_links: dict[str, str] = {}
    changed = [r for r in results if r.outcome == "changed"]
    by_race: dict[str, tuple[Race, list[CheckResult]]] = {}
    for r in changed:
        for race in r.page.races:
            by_race.setdefault(race.id, (race, []))[1].append(r)
    to_propose = {race_id: race for race_id, (race, _) in by_race.items()}
    if args.propose_for:
        match = [race for race in races if race.id == args.propose_for]
        if not match:
            print(f"no race with id {args.propose_for}", file=sys.stderr)
            return 1
        to_propose[match[0].id] = match[0]

    outcomes: dict[str, Outcome] = {}
    if pulls and llm:
        for race in to_propose.values():
            race_results = by_race.get(race.id, (race, []))[1]
            outcome = propose_update(race, store, {r.page.url: r.diff for r in race_results}, llm, pulls, now)
            outcomes[race.id] = outcome
            if outcome.kind == "cleared":
                for url in race_urls(race):
                    if url in store.pages_by_url():
                        store.get(Page(url, [race])).last_cleared = now.isoformat(timespec="seconds")
            if args.propose_for == race.id:
                print(f"Proposal for {race.id}: {outcome.url or outcome.note.strip() or outcome.kind}")
        store.save()

    for race_id, (race, race_results) in by_race.items():
        outcome = outcomes.get(race_id, Outcome("review"))
        link = outcome.url if outcome.kind == "pr" else None
        if link is None and issues:
            link = issues.report_change(race, race_results, now, outcome.note, cleared=outcome.kind == "cleared")
        elif link is None:
            for r in race_results:
                print(f"\n=== Changed: {race.name} ({r.page.url})")
                print("\n".join(r.diff))
        for r in race_results:
            if link:
                issue_links[r.page.url] = link

    for result in results:
        link = None
        if result.outcome == "failed" and result.failing_days >= args.alert_after_days:
            if issues:
                link = issues.report_unreachable(result)
            else:
                print(f"\n=== Unreachable for {result.failing_days} days: {result.page.label} ({result.detail})")
        if result.recovered and issues:
            link = issues.close_unreachable(result) or link
        if link:
            issue_links[result.page.url] = link

    report = summary(results, issue_links)
    print(report)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    return 0


@dataclass
class Outcome:
    """What the LLM check decided for one race."""

    kind: str  # "pr": proposed in a pull request; "cleared": nothing changed; "review": a person should look
    url: str | None = None
    note: str = ""


def propose_update(
    race: Race, store: StateStore, diffs: dict[str, list[str]], llm: JsonLLM, pulls: GitHubPulls, now: datetime
) -> Outcome:
    """Run the LLM on the race's pages and open a pull request if it finds new values."""
    pages = {url: store.read_text(Page(url, [race])) for url in race_urls(race)}
    pages = {url: text for url, text in pages.items() if text}
    if not pages:
        return Outcome("review")
    try:
        extraction = extract(race, pages, llm, now.date())
    except LLMError as exc:
        return Outcome("review", note=f"**LLM check for {race.name}:** couldn't run ({exc}).\n\n")

    proposal = build_proposal(extraction, pages, now.date())
    if not proposal.has_changes:
        if not proposal.for_a_person:
            return Outcome("cleared", note=f"**LLM check for {race.name}:** no changes to the race details found.\n\n")
        for_a_person = "".join(f"- {n}\n" for n in proposal.for_a_person)
        return Outcome(
            "review",
            note=f"**LLM check for {race.name}:** no changes it could make itself, but check these:\n{for_a_person}\n",
        )

    try:
        new_text = update_race(pulls.read_data_file(), race.id, proposal.updates)
        parse_races(new_text)
    except (KeyError, DataError) as exc:
        return Outcome("review", note=f"**LLM check for {race.name}:** proposed changes didn't pass validation ({exc}).\n\n")
    except httpx.HTTPError as exc:
        return Outcome("review", note=f"**LLM check for {race.name}:** couldn't read races.yaml from GitHub ({exc}).\n\n")
    try:
        return Outcome("pr", url=pulls.propose(race.id, new_text, pr_title(proposal), pr_body(proposal, diffs, llm.model)))
    except httpx.HTTPError as exc:
        changes = "".join(f"- `{c.name}`: {format_cell(c.old)} → {format_cell(c.new)} (“{c.quote}”)\n" for c in proposal.changes)
        return Outcome("review", note=(
            f"**LLM check for {race.name}:** proposed these changes, but the pull request couldn't be opened "
            f"({exc}):\n{changes}\n"
        ))


if __name__ == "__main__":
    sys.exit(main())
