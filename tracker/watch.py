"""Daily change detection for official race pages.

State is kept between runs in the GitHub Actions cache rather than the
repo, so the race sites' text isn't republished:

    state/pages.json         one entry per watched URL (hash, timestamps, failures)
    state/pages/<key>.txt    the cleaned text last seen, used to show diffs
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from tracker.clean import clean_html
from tracker.fetch import FetchResult
from tracker.models import Monitoring, Race, Status

DIFF_CONTEXT = 1
MAX_DIFF_LINES = 80


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...


@dataclass
class Page:
    """A watched URL and the races that point at it."""

    url: str
    races: list[Race]

    @property
    def key(self) -> str:
        return page_key(self.url)

    @property
    def label(self) -> str:
        return ", ".join(race.name for race in self.races)


@dataclass
class PageState:
    url: str
    hash: str | None = None
    last_checked: str | None = None
    last_ok: str | None = None
    last_changed: str | None = None
    last_result: str | None = None
    consecutive_failures: int = 0
    failing_since: str | None = None  # first failure in the current run of failures
    last_cleared: str | None = None  # when the LLM last found no race details changed on this page


@dataclass
class CheckResult:
    page: Page
    outcome: str  # "baseline", "unchanged", "changed", "failed"
    detail: str = ""
    diff: list[str] = field(default_factory=list)
    failing_days: int = 0  # whole days since failures started (failed pages only)
    recovered: bool = False  # page works again after failing


def page_key(url: str) -> str:
    key = re.sub(r"^https?://(www\.)?", "", url).strip("/")
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")


def race_urls(race: Race) -> list[str]:
    """The pages that describe a race: its official page, plus its source page if that's different."""
    urls = [str(u) for u in (race.official_url, race.source_url) if u is not None]
    return list(dict.fromkeys(urls))


def watched_pages(races: list[Race]) -> list[Page]:
    """Pages of races that are monitored automatically and not yet done."""
    pages: dict[str, Page] = {}
    for race in races:
        if race.monitoring != Monitoring.auto or race.status == Status.done:
            continue
        for url in race_urls(race):
            pages.setdefault(url, Page(url, [])).races.append(race)
    return list(pages.values())


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.index_path = root / "pages.json"
        self.text_dir = root / "pages"
        raw = json.loads(self.index_path.read_text(encoding="utf-8")) if self.index_path.exists() else {}
        self.pages = {key: PageState(**value) for key, value in raw.items()}

    def pages_by_url(self) -> dict[str, PageState]:
        return {state.url: state for state in self.pages.values()}

    def get(self, page: Page) -> PageState:
        return self.pages.setdefault(page.key, PageState(url=page.url))

    def read_text(self, page: Page) -> str | None:
        path = self.text_dir / f"{page.key}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_text(self, page: Page, text: str) -> None:
        self.text_dir.mkdir(parents=True, exist_ok=True)
        (self.text_dir / f"{page.key}.txt").write_text(text + "\n", encoding="utf-8", newline="\n")

    def prune(self, keep: set[str]) -> list[str]:
        """Forget pages that are no longer watched, including their saved text. Returns the removed keys."""
        removed = sorted(key for key in self.pages if key not in keep)
        for key in removed:
            del self.pages[key]
        if self.text_dir.exists():
            for path in self.text_dir.glob("*.txt"):
                if path.stem not in keep:
                    path.unlink()
                    if path.stem not in removed:
                        removed.append(path.stem)
        return removed

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {key: vars(state) for key, state in sorted(self.pages.items())}
        self.index_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_pages(pages: list[Page], fetcher: PageFetcher, store: StateStore, now: datetime) -> list[CheckResult]:
    stamp = now.isoformat(timespec="seconds")
    results = []
    for page in pages:
        state = store.get(page)
        state.url = page.url
        state.last_checked = stamp
        fetched = fetcher.fetch(page.url)

        text = clean_html(fetched.html) if fetched.ok else ""
        if not fetched.ok or not text:
            state.consecutive_failures += 1
            state.failing_since = state.failing_since or stamp
            state.last_result = fetched.describe() if not fetched.ok else "empty page"
            days = (now.date() - datetime.fromisoformat(state.failing_since).date()).days
            results.append(CheckResult(page, "failed", state.last_result, failing_days=days))
            continue

        recovered = state.failing_since is not None
        state.last_ok = stamp
        state.last_result = "ok"
        state.consecutive_failures = 0
        state.failing_since = None
        new_hash = text_hash(text)
        previous = store.read_text(page)

        if state.hash is None or previous is None:
            outcome, diff = "baseline", []
        elif new_hash == state.hash:
            outcome, diff = "unchanged", []
        else:
            outcome, diff = "changed", text_diff(previous, text)
            state.last_changed = stamp

        if outcome != "unchanged":
            store.write_text(page, text)
        state.hash = new_hash
        results.append(CheckResult(page, outcome, diff=diff, recovered=recovered))

    # Pages we stop watching (e.g. a race switched to manual) shouldn't keep a copy of their text.
    store.prune({page.key for page in pages})
    store.save()
    return results


def text_diff(old: str, new: str) -> list[str]:
    lines = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="before", tofile="after", n=DIFF_CONTEXT, lineterm=""
        )
    )[2:]  # drop the ---/+++ header
    if len(lines) > MAX_DIFF_LINES:
        hidden = len(lines) - MAX_DIFF_LINES
        lines = lines[:MAX_DIFF_LINES] + [f"... {hidden} more lines not shown"]
    return lines
