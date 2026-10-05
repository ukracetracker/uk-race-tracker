"""How current is each race's information, combining human verification with the daily page check.

`last_verified` in races.yaml only changes when a person confirms values (by
hand or by merging a bot pull request). The daily check adds what it knows:
whether the race's pages loaded, and whether they've changed since then in a
way nobody has reviewed (the LLM clears changes that leave the race's details
as they were).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from tracker.models import Monitoring, Race
from tracker.watch import PageState, race_urls

STALE_AFTER_DAYS = 3  # pages unreadable for longer than this no longer count as "checked"


class Freshness(str, Enum):
    unchanged = "unchanged"  # every page read recently, none changed since verification
    changed = "changed"  # a page changed after the race was last verified
    by_hand = "by_hand"  # manual monitoring, or the bot can't currently read the pages
    unknown = "unknown"  # no daily-check record yet


@dataclass(frozen=True)
class RaceFreshness:
    status: Freshness
    text: str


def race_freshness(race: Race, states: dict[str, PageState], today: date) -> RaceFreshness:
    if race.monitoring == Monitoring.manual:
        return RaceFreshness(Freshness.by_hand, "checked by hand")
    pages = [states.get(url) for url in race_urls(race)]
    if not pages or any(state is None for state in pages):
        return RaceFreshness(Freshness.unknown, "")

    changed = [_day(s.last_changed) for s in pages if s.last_changed]
    # A change counts as reviewed once a person verifies the race, or the LLM read the
    # pages and found none of the race's details had changed.
    reviewed = [race.last_verified] if race.last_verified else []
    reviewed += [_day(s.last_cleared) for s in pages if s.last_cleared]
    if changed and (not reviewed or max(changed) > max(reviewed)):
        return RaceFreshness(Freshness.changed, f"page changed {_fmt(max(changed))}: being reviewed")

    oks = [_day(s.last_ok) for s in pages if s.last_ok]
    if len(oks) < len(pages) or (today - min(oks)).days > STALE_AFTER_DAYS:
        return RaceFreshness(Freshness.by_hand, "site blocks the daily check: checked by hand")
    return RaceFreshness(Freshness.unchanged, f"page unchanged, checked {_fmt(min(oks))}")


def states_by_url(states: dict[str, PageState]) -> dict[str, PageState]:
    return {state.url: state for state in states.values()}


def _day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).date()


def _fmt(day: date) -> str:
    # A date, not "today": the page is built once a day and read any time after.
    return day.strftime("%d %b")
