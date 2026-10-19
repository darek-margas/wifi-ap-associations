"""Presence across poll rounds: the state a program keeps between polls, and the loops.

PresenceTracker is what the Home Assistant integration's coordinator keeps, without
Home Assistant: feed it each poll round's reads and it keeps the sightings (with their
visits), each access point's state and the coverage counts, rides out a short total
outage, and gives the data to save and to restore. The program around it schedules the
polls, writes the storage and decides what to show; poll_all() and watch_forever() are
the polling and instant-arrival loops it can use.

Only the standard library and this package, so a program without Home Assistant (e.g.
an MQTT bridge) gets the same presence rules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import time
from typing import Any

from . import AccessPointDriver, AccessPointError, PollResult
from .coverage import AccessPointCoverage, CoverageTracker, access_point_coverage
from .presence import (
    AccessPointRead,
    AccessPointState,
    Sighting,
    carry_visits,
    is_present,
    merge_reads,
    prune,
    restart_visit_gap,
    ride_out_total_failure,
    sightings_from_storage,
    sightings_to_storage,
    stored_at,
    stored_cleanly,
)

_LOGGER = logging.getLogger(__name__)

# Sightings are kept this long, so devices that sleep with Wi-Fi off (cars, tablets) are
# still known when someone picks devices to track.
SIGHTING_RETENTION = timedelta(days=7)
# A device read in the latest poll must count as present, so a shorter grace period
# acts as this one: away after one missed poll.
MIN_CONSIDER_HOME = timedelta(seconds=30)
# Fast departure is never quicker than this after a reported leave.
MIN_FAST_DEPARTURE = timedelta(seconds=10)
DEFAULT_POLL_INTERVAL = timedelta(seconds=60)
# Sightings this close to the poll interval apart still belong to the same visit, even
# with a short grace period (polls are an interval apart, give or take a slow AP).
VISIT_GAP_SLACK = timedelta(seconds=30)

# Instant arrivals: a failing watch is tried again after a growing wait; one that ran
# this long counts as working, so the wait starts small again.
WATCH_RETRY_MIN = 5
WATCH_RETRY_MAX = 300
WATCH_STABLE = 600


class AllAccessPointsFailed(Exception):
    """No access point could be read, and the last good poll is past the grace period:
    presence is unknown (not everyone away)."""


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    """Sightings by MAC and access point state by access point id, after a poll round."""

    sightings: dict[str, Sighting]
    access_points: dict[str, AccessPointState]


class PresenceTracker:
    """The presence state across poll rounds (see the module docstring).

    Fast departure (opt-in, fast_departure set): a device whose access point reported a
    client leaving (note_leave(), from a watch's on_leave) after the device was last seen,
    and which a later successful round of that access point didn't see, is away
    fast_departure after that report instead of after the grace period. Faster, at a
    price: a device that drops off for longer than that and comes back (power saving, a
    reconnect) shows away meanwhile. Access points report a leave without saying which
    client, so any device missing from that access point after a leave counts.
    """

    def __init__(
        self,
        consider_home: timedelta,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        retention: timedelta = SIGHTING_RETENTION,
        fast_departure: timedelta | None = None,
    ) -> None:
        """consider_home: the grace period; poll_interval: how often the program polls;
        fast_departure: off (None) or how long after a reported leave (see above)."""
        self.consider_home = max(consider_home, MIN_CONSIDER_HOME)
        self.fast_departure = (
            max(fast_departure, MIN_FAST_DEPARTURE) if fast_departure else None
        )
        # Recent reports of a client leaving, per access point (within the grace period).
        self._leaves: dict[str, list[datetime]] = {}
        # Fast departures decided by a round: away until seen again, whatever is reported
        # or read afterwards (another client's leave, the access point failing).
        self._departed: dict[str, datetime] = {}
        # When each device was last observed: the start of the round that saw it. A round
        # takes time; a leave reported during it may come after the access point's read.
        self._observed: dict[str, datetime] = {}
        # Start of the last round in which an access point was read.
        self._last_round_started: datetime | None = None
        self.visit_gap = max(self.consider_home, poll_interval + VISIT_GAP_SLACK)
        self.retention = retention
        self.sightings: dict[str, Sighting] = {}
        self.access_points: dict[str, AccessPointState] = {}
        # Coverage: per-device counts of the day, per-AP client signal.
        self.coverage = CoverageTracker()
        self.access_point_coverage: dict[str, AccessPointCoverage] = {}
        # Last round in which at least one access point was read; a round in which none
        # could be is ridden out only within the grace period after it.
        self.last_success: datetime | None = None
        # When the restored data was written, and whether at stop (clean); None once
        # the first round used it.
        self._restored_at: datetime | None = None
        self._restored_clean = False

    def snapshot(self) -> PresenceSnapshot:
        """The current sightings and access point states (copies)."""
        return PresenceSnapshot(dict(self.sightings), dict(self.access_points))

    def restore(self, stored: Mapping[str, Any] | None, now: datetime, today: date) -> None:
        """Load what to_storage() returned before the program last stopped."""
        self.sightings = sightings_from_storage(stored, now, self.retention)
        self._departed = {}
        for mac, at in ((stored or {}).get("departed") or {}).items():
            try:
                when = datetime.fromisoformat(at)
            except (TypeError, ValueError):
                continue
            if mac in self.sightings and when.tzinfo is not None:
                self._departed[mac] = when
        self._restored_at = stored_at(stored)
        self._restored_clean = stored_cleanly(stored)
        # Today's coverage counts (roams, drops, late roams) survive a same-day restart.
        self.coverage.restore((stored or {}).get("coverage"), today)

    def to_storage(self, now: datetime, clean: bool = False) -> dict[str, Any]:
        """What to save: the sightings and today's coverage counts, JSON-friendly.

        clean: written as the program stops, so current until now (see presence.py).
        """
        data = sightings_to_storage(self.sightings, now, clean=clean)
        data["coverage"] = self.coverage.to_storage()
        if self._departed:
            data["departed"] = {mac: at.isoformat() for mac, at in self._departed.items()}
        return data

    def update(
        self,
        reads: Iterable[AccessPointRead],
        now: datetime,
        today: date,
        started: datetime | None = None,
    ) -> PresenceSnapshot:
        """Take one poll round of every configured access point.

        Access points that fail keep their last sightings (their clients stay home
        within the grace period) and become unavailable. If none could be read, the round
        is ridden out within the grace period after the last good one; past it,
        AllAccessPointsFailed is raised. `today` is the local date, for the daily counts;
        `started` when the round began (the reads happened between it and `now`).
        """
        started = started or now
        reads = list(reads)
        self.coverage.reset_day(today)
        if not reads:
            return self.snapshot()
        merged = merge_reads(reads, now)
        self.access_points.update(merged.states)
        for access_point_id in merged.failed:
            # Keep what the AP last reported about itself (name, firmware) while down.
            previous = self.access_points.get(access_point_id)
            self.access_points[access_point_id] = AccessPointState(
                available=False, info=previous.info if previous else None
            )
        if len(merged.failed) == len(reads):
            # Nobody was observed: a device seen on another AP afterwards is no roam.
            self.coverage.interrupt()
            # A short total outage (e.g. a controller restart) is ridden out on the
            # previous sightings, like a single failing AP.
            if not ride_out_total_failure(self.last_success, now, self.consider_home):
                raise AllAccessPointsFailed("None of the access points could be read")
            return self.snapshot()
        # Departures due by now, from the rounds before this one: settled before this
        # round's sightings, so a device back now starts a new visit.
        self._confirm_departures(now)
        self.last_success = now
        self._last_round_started = started
        self.coverage.update(merged.sightings, merged.failed, now, today, self.consider_home)
        self.access_point_coverage = access_point_coverage(merged.sightings)

        visit_gap = self.visit_gap
        if self._restored_at is not None:
            # First round after a restart (see restart_visit_gap).
            visit_gap = restart_visit_gap(visit_gap, self._restored_at, self._restored_clean, now)
            self._restored_at = None
        # A device that departed (fast) and is seen again arrives anew.
        previous = {mac: s for mac, s in self.sightings.items() if mac not in self._departed}
        seen = carry_visits(merged.sightings, previous, now, visit_gap)
        for mac in seen:
            self._departed.pop(mac, None)
            self._observed[mac] = started
        self.sightings = prune({**self.sightings, **seen}, now, self.retention)
        self._departed = {m: at for m, at in self._departed.items() if m in self.sightings}
        self._observed = {m: at for m, at in self._observed.items() if m in self.sightings}
        # Reports older than the grace period can't shorten anything any more.
        self._leaves = {
            ap: kept
            for ap, times in self._leaves.items()
            if (kept := [at for at in times if now - at < self.consider_home])
        }
        # Departures this round settles (missing from an access point read after a leave).
        self._confirm_departures(now)
        return self.snapshot()

    def _confirm_departures(self, now: datetime) -> None:
        for mac, sighting in self.sightings.items():
            if mac not in self._departed and (gone := self._left_fast(mac, sighting, now)):
                self._departed[mac] = gone

    def note_leave(self, access_point_id: str, at: datetime) -> None:
        """An access point reported that a client left it (for fast departure)."""
        self._leaves.setdefault(access_point_id, []).append(at)

    def _left_fast(self, mac: str, sighting: Sighting, now: datetime) -> datetime | None:
        """When the device counts as away by fast departure (not yet settled), or None.

        Its access point reported a leave no earlier than the device was last observed,
        a round that started after that report read the access point and didn't see the
        device, and fast_departure has passed since the report.
        """
        if self.fast_departure is None:
            return None
        state = self.access_points.get(sighting.access_point_id)
        if (
            self.last_success is None
            or self._last_round_started is None
            or sighting.last_seen >= self.last_success  # seen in the latest round
            or state is None
            or not state.available  # its access point wasn't read: no evidence
        ):
            return None
        observed = self._observed.get(mac, sighting.last_seen)
        leaves = [
            at
            for at in self._leaves.get(sighting.access_point_id, ())
            if observed <= at < self._last_round_started
        ]
        if not leaves:
            return None
        gone = min(leaves) + self.fast_departure
        return gone if now >= gone else None

    def current(self, mac: str | None, now: datetime) -> Sighting | None:
        """The MAC's sighting if it is home (within the grace period, and not gone by
        fast departure), else None."""
        if not mac:
            return None
        sighting = self.sightings.get(mac)
        if not is_present(sighting, now, self.consider_home):
            return None
        assert sighting is not None
        if mac in self._departed or self._left_fast(mac, sighting, now):
            return None
        return sighting

    def away_since(self, mac: str, now: datetime) -> datetime | None:
        """When a device that isn't home counted as away (None if never seen)."""
        sighting = self.sightings.get(mac)
        if sighting is None:
            return None
        fast = self._departed.get(mac) or self._left_fast(mac, sighting, now)
        return fast or sighting.last_seen + self.consider_home


async def poll_all(drivers: Sequence[AccessPointDriver]) -> list[PollResult | BaseException]:
    """Poll every access point at once, each within its POLL_TIMEOUT.

    One result per driver, in order: its PollResult, or the exception it raised (a
    timeout is a TimeoutError). Cancellation of the caller still cancels.
    """
    return await asyncio.gather(
        *(asyncio.wait_for(driver.async_poll(), driver.POLL_TIMEOUT) for driver in drivers),
        return_exceptions=True,
    )


async def watch_forever(
    driver: AccessPointDriver,
    on_change: Callable[[], None],
    *,
    on_leave: Callable[[], None] | None = None,
    on_paused: Callable[[str], None] | None = None,
    on_resumed: Callable[[], None] | None = None,
    on_failed_again: Callable[[str], None] | None = None,
    retry_min: float = WATCH_RETRY_MIN,
    retry_max: float = WATCH_RETRY_MAX,
    stable: float = WATCH_STABLE,
) -> None:
    """Keep one access point's change feed open (instant arrivals) until cancelled.

    on_change() only asks for a poll; the poll decides who is there. on_leave(), if
    given, is passed to the driver: a client left this access point (for fast
    departure, PresenceTracker.note_leave()). A feed that ends or fails is started
    again after a growing wait (retry_min seconds, doubling up to retry_max); one that
    ran `stable` seconds counts as working. on_paused(reason) is called once when it
    stops working, on_resumed() once when it works again, and on_failed_again(reason)
    for each failed retry in between.
    """
    delay = retry_min
    paused = False
    while True:
        started = time.monotonic()
        try:
            if on_leave is None:
                await driver.async_watch(on_change)
            else:
                await driver.async_watch(on_change, on_leave=on_leave)
            reason = "the feed ended"
        except asyncio.CancelledError:
            raise
        except AccessPointError as err:
            reason = str(err) or type(err).__name__
        except Exception:  # a driver bug must not end the watching for good
            _LOGGER.exception("Unexpected error watching %s", driver.TYPE)
            reason = "unexpected error"
        if time.monotonic() - started >= stable:
            delay = retry_min
            if paused:
                paused = False
                if on_resumed is not None:
                    on_resumed()
        if not paused:
            paused = True
            if on_paused is not None:
                on_paused(reason)
        elif on_failed_again is not None:
            on_failed_again(reason)
        await asyncio.sleep(delay)
        delay = min(delay * 2, retry_max)
