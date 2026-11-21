"""A scout session with a fake browser: signals out, pacing honoured, halts on warnings."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from oo_scout.browser import WarningPage, classify
from oo_scout.pacing import PAGE_VIEW
from oo_scout.playbook import PlaybookError, Rule, Playbook, parse
from oo_scout.scout import Scout, run_forever


class FakeSession:
    def __init__(self, warn_on=None):
        self.page = SimpleNamespace(url="about:blank")
        self.visited = []
        self.closed = 0
        self.warn_on = warn_on
        self.self_profile = {"url": "https://www.linkedin.com/in/self/"}

    def goto(self, url):
        self.page.url = url
        self.visited.append(url)
        self.check()

    def check(self):
        if self.warn_on and self.warn_on in self.page.url:
            raise WarningPage("checkpoint", self.page.url)

    def pause(self):
        self.pauses = getattr(self, "pauses", 0) + 1

    def close(self):
        self.closed += 1


class FakeUpstream:
    def __init__(self, session, n=3):
        self.session, self.n = session, n

    def search_people(self, session, keyword, network=None):
        session.page.url = f"https://www.linkedin.com/search/results/people/?keywords={keyword}"
        return {"profiles": [{"public_identifier": f"p{i}", "url": f"https://www.linkedin.com/in/p{i}/"}
                             for i in range(self.n)] + [{"url": "https://www.linkedin.com/in/self/"}]}

    def scrape_profile(self, session, profile):
        session.page.url = profile["url"]
        return {"url": profile["url"], "full_name": profile["public_identifier"], "positions": []}, {}

    def extract_in_urls(self, page):
        return ["https://www.linkedin.com/in/e1/", "https://www.linkedin.com/in/self/", "https://www.linkedin.com/in/e2/"]


class ListSink:
    def __init__(self):
        self.rows = []

    def write(self, signals):
        self.rows += signals
        return len(signals)


PB = Playbook((
    Rule("icp", people_keywords=("platform engineer",), max_profiles_per_search=5),
    Rule("engagers", watch_posts=("https://www.linkedin.com/feed/update/urn:li:activity:1/",)),
))


def make(pacer, warn_on=None, n=3):
    session = FakeSession(warn_on)
    alerts = []
    scout = Scout(session, pacer, ListSink(), alerts.append, upstream=FakeUpstream(session, n))
    return scout, session, alerts


def test_session_emits_profile_and_engager_signals(pacer):
    scout, session, alerts = make(pacer)
    stats = scout.run_session(PB)
    assert stats.stopped == "done" and not alerts
    types = [(s.signal_type, s.rule_id) for s in scout.sink.rows]
    assert types.count(("trigger_event", "icp")) == 3
    assert types.count(("engagement", "engagers")) == 2  # self excluded
    # feed + search + 3 profiles + 1 post
    assert stats.page_views == 6 == len(pacer.ledger.events(PAGE_VIEW))
    assert session.closed == 1


def test_second_session_skips_seen_profiles(pacer):
    scout, *_ = make(pacer)
    scout.run_session(PB)
    scout2, *_ = make(pacer)
    scout2.run_session(PB)
    assert len(scout2.sink.rows) == 0


def test_page_view_cap_ends_the_session_without_more_views(pacer):
    for _ in range(48):
        pacer.acquire(PAGE_VIEW)
    scout, *_ = make(pacer)
    stats = scout.run_session(PB)
    assert stats.stopped == "cap_reached"
    assert len(pacer.ledger.events(PAGE_VIEW)) == 50


def test_checkpoint_halts_alerts_and_rests(pacer):
    scout, session, alerts = make(pacer, warn_on="/search/")
    stats = scout.run_session(PB)
    assert stats.stopped == "checkpoint"
    assert pacer.ledger.halt["reason"] == "checkpoint"
    assert len(alerts) == 1 and "resume" in alerts[0]
    assert scout.sink.rows == []


def test_out_of_hours_session_does_not_open_browser(pacer, clock):
    clock.now = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)  # Saturday
    scout, session, _ = make(pacer)
    with pytest.raises(Exception):
        scout.run_session(PB)
    assert session.visited == [] and session.closed == 0


def test_run_forever_sleeps_out_of_hours_and_keeps_last_playbook(pacer, clock):
    clock.now = datetime(2026, 11, 27, 17, 30, tzinfo=UTC)  # Friday 18:30
    slept, alerts = [], []

    def sleep(s):
        slept.append(s)
        clock.now = datetime(2026, 11, 30, 8, 0, tzinfo=UTC)  # Monday 09:00

    loads = iter([PB])

    def load():
        try:
            return next(loads)
        except StopIteration:
            raise PlaybookError("broken")

    made = []

    def make_scout():
        s, *_ = make(pacer)
        made.append(s)
        return s

    run_forever(make_scout, pacer, load, alerts.append, sleep=sleep, max_sessions=2)
    assert slept[0] >= (datetime(2026, 11, 30, 7, 30, tzinfo=UTC) - datetime(2026, 11, 27, 17, 30, tzinfo=UTC)).total_seconds()
    assert len(made) == 2
    assert any("keeping last valid version" in a for a in alerts)


@pytest.mark.parametrize("url,text,reason", [
    ("https://www.linkedin.com/checkpoint/challenge/AgF", "", "checkpoint"),
    ("https://www.linkedin.com/login?session_redirect=%2Ffeed%2F", "", "logged_out"),
    ("https://www.linkedin.com/authwall?trk=x", "", "logged_out"),
    ("https://www.linkedin.com/feed/?captcha=1", "", "captcha"),
    ("https://www.linkedin.com/feed/", "Let's do a quick security check", "warning_page"),
    ("https://www.linkedin.com/feed/", "Your account has been restricted", "warning_page"),
    ("https://www.linkedin.com/feed/", "Jane posted about Kubernetes", None),
    ("https://www.linkedin.com/in/jane/", "", None),
])
def test_classify(url, text, reason):
    assert classify(url, text) == reason


def test_playbook_parse_and_bounds():
    pb = parse({"linkedin": {"rules": [
        {"id": "a", "people_keywords": ["x"], "network": ["second"], "max_profiles_per_search": 3},
        {"id": "b", "watch_posts": ["https://www.linkedin.com/feed/update/1/"]},
        {"id": "c", "watch_posts": ["https://linkedin.com/posts/acme_activity-1-x/"]},
    ]}})
    assert pb.rules[0].network == ("S",) and pb.rules[1].watch_posts
    for bad in (
        {"linkedin": {"rules": [{"id": "a"}]}},
        {"linkedin": {"rules": [{"id": "a", "people_keywords": ["x"], "max_profiles_per_search": 11}]}},
        {"linkedin": {"rules": [{"id": "a", "people_keywords": ["x"], "network": ["fourth"]}]}},
        {"linkedin": {"rules": [{"id": "a", "people_keywords": ["x"]}, {"id": "a", "people_keywords": ["y"]}]}},
        {"linkedin": {"rules": [{"id": "a", "watch_posts": ["https://evil.example/"]}]}},
        {"linkedin": {"rules": [{"id": "a", "watch_posts": ["https://evil.example/?linkedin.com/"]}]}},
        {"linkedin": {"rules": [{"id": "a", "watch_posts": ["https://linkedin.com.evil.example/feed/update/1/"]}]}},
        {"linkedin": {"rules": [{"id": "a", "watch_posts": ["http://www.linkedin.com/feed/update/1/"]}]}},
        {"linkedin": {"rules": [{"id": "a", "watch_posts": ["https://www.linkedin.com/in/someone/"]}]}},
        {"linkedin": {"rules": [{"id": "a", "watch_posts": "https://www.linkedin.com/feed/update/1/"}]}},
        {"linkedin": {"rules": [{"id": "a", "people_keywords": "platform"}]}},
        {"linkedin": {"rules": [{"id": "a", "people_keywords": ["x"], "network": "second"}]}},
    ):
        with pytest.raises(PlaybookError):
            parse(bad)


class FakeLink:
    def __init__(self, href):
        self.href = href

    def get_attribute(self, name):
        return self.href


class FakeLocator:
    def __init__(self, hrefs):
        self.hrefs = hrefs

    def count(self):
        return 1

    def all(self):
        return [FakeLink(h) for h in self.hrefs]

    def locator(self, selector):
        return FakeLocator(self.hrefs)


class FakePostPage:
    url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"

    def __init__(self, main, everywhere):
        self.main, self.everywhere = main, everywhere

    def locator(self, selector):
        return FakeLocator(self.main) if selector == "main" else FakeLocator(self.everywhere)


def test_engagers_come_from_the_main_column_only():
    from oo_scout.scout import Upstream

    page = FakePostPage(main=["/in/engager/", "/in/engager/?miniProfileUrn=x"],
                        everywhere=["/in/engager/", "/in/sidebar-suggestion/", "/in/top-bar-me/"])
    urls = Upstream().extract_in_urls(page)
    assert [u.rstrip("/").rsplit("/", 1)[-1] for u in urls] == ["engager"]


def test_repeated_engager_on_a_post_is_one_signal(pacer):
    scout, session, _ = make(pacer)
    scout.up.extract_in_urls = lambda page: ["https://www.linkedin.com/in/e1/",
                                             "https://www.linkedin.com/in/e1/?x=1"]
    scout.run_session(Playbook((Rule("w", watch_posts=("https://www.linkedin.com/feed/update/1/",)),)))
    assert [r.actor_key for r in scout.sink.rows] == ["https://www.linkedin.com/in/e1/"]
