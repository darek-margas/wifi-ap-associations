"""Tests for PresenceTracker, poll_all() and watch_forever(): presence across poll rounds.

The same behaviour the Home Assistant integration's own tests check through Home
Assistant, here without it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import json

import pytest

from wifi_ap_associations import (
    AccessPointDriver,
    AccessPointError,
    AccessPointInfo,
    AssociatedClient,
    DriverField,
    PollResult,
)
from wifi_ap_associations.presence import SIGNAL_DBM, AccessPointRead
from wifi_ap_associations.tracker import (
    MIN_CONSIDER_HOME,
    AllAccessPointsFailed,
    PresenceTracker,
    poll_all,
    watch_forever,
)

T0 = datetime(2026, 10, 20, 8, 0, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)
POLL = timedelta(seconds=60)
GRACE = timedelta(minutes=3)
PHONE = "5C:AD:BA:00:00:01"
LAPTOP = "5C:AD:BA:00:00:02"


def poll(*clients: tuple[str, int], name: str = "Hall", uptime: int | None = 3600) -> PollResult:
    return PollResult(
        [AssociatedClient(mac, "Home", "5GHz", signal) for mac, signal in clients],
        AccessPointInfo(name=name, firmware="1.0", uptime_seconds=uptime),
    )


def read(ap: str, result: PollResult | BaseException) -> AccessPointRead:
    return AccessPointRead(ap, ap.title(), SIGNAL_DBM, result)


def test_a_round_gives_sightings_states_and_coverage() -> None:
    tracker = PresenceTracker(GRACE)
    snap = tracker.update(
        [read("hall", poll((PHONE, -50))), read("den", poll((PHONE, -70), (LAPTOP, -80)))],
        T0, TODAY,
    )
    # A device on two APs goes to the stronger one.
    assert snap.sightings[PHONE].access_point_id == "hall"
    assert snap.sightings[PHONE].arrived == T0
    assert snap.sightings[LAPTOP].access_point_id == "den"
    assert snap.access_points["hall"].available and snap.access_points["den"].clients_by_band == {"5GHz": 2}
    assert tracker.access_point_coverage["den"].weak_clients == 1
    assert tracker.current(PHONE, T0 + GRACE - timedelta(seconds=1)) is not None
    assert tracker.current(PHONE, T0 + GRACE) is None
    assert tracker.current(None, T0) is None


def test_visit_continues_across_rounds_and_restarts_after_leaving() -> None:
    tracker = PresenceTracker(GRACE)
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    tracker.update([read("hall", poll((PHONE, -50)))], T0 + POLL, TODAY)
    assert tracker.sightings[PHONE].arrived == T0
    tracker.update([read("hall", poll())], T0 + 2 * POLL, TODAY)  # gone
    later = T0 + 2 * POLL + GRACE + timedelta(minutes=5)
    tracker.update([read("hall", poll((PHONE, -50)))], later, TODAY)
    assert tracker.sightings[PHONE].arrived == later


def test_a_failing_ap_keeps_its_info_and_its_clients_stay_home() -> None:
    tracker = PresenceTracker(GRACE)
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], T0, TODAY)
    snap = tracker.update(
        [read("hall", AccessPointError("down")), read("den", poll())], T0 + POLL, TODAY
    )
    assert not snap.access_points["hall"].available
    assert snap.access_points["hall"].info.name == "Hall"
    assert tracker.current(PHONE, T0 + POLL) is not None  # last seen within grace


def test_total_outage_is_ridden_out_within_grace_then_raises() -> None:
    tracker = PresenceTracker(GRACE)
    with pytest.raises(AllAccessPointsFailed):  # never before a good round
        tracker.update([read("hall", AccessPointError("down"))], T0, TODAY)
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    snap = tracker.update([read("hall", AccessPointError("down"))], T0 + POLL, TODAY)
    assert PHONE in snap.sightings and not snap.access_points["hall"].available
    with pytest.raises(AllAccessPointsFailed):
        tracker.update([read("hall", AccessPointError("down"))], T0 + GRACE, TODAY)


def test_no_access_points_is_an_empty_round() -> None:
    assert PresenceTracker(GRACE).update([], T0, TODAY).sightings == {}


def test_grace_and_visit_gap_floors() -> None:
    tracker = PresenceTracker(timedelta(seconds=5), poll_interval=POLL)
    assert tracker.consider_home == MIN_CONSIDER_HOME
    assert tracker.visit_gap == POLL + timedelta(seconds=30)
    assert PresenceTracker(timedelta(minutes=10)).visit_gap == timedelta(minutes=10)


def test_storage_round_trip_keeps_visits_across_a_clean_restart() -> None:
    tracker = PresenceTracker(GRACE)
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    tracker.update([read("hall", poll((PHONE, -50), (LAPTOP, -60)))], T0 + POLL, TODAY)
    tracker.update([read("den", poll((PHONE, -50))), read("hall", poll((LAPTOP, -60)))], T0 + 2 * POLL, TODAY)
    assert tracker.coverage.counts[PHONE].roams == 1
    stopped = T0 + 2 * POLL + timedelta(seconds=10)
    saved = json.loads(json.dumps(tracker.to_storage(stopped, clean=True)))  # as a file would

    # Down for an hour, far longer than the grace period: after a clean stop that is
    # no evidence of anyone leaving, so the visit goes on.
    back = stopped + timedelta(hours=1)
    restarted = PresenceTracker(GRACE)
    restarted.restore(saved, back, TODAY)
    assert restarted.sightings[PHONE].arrived == T0
    assert restarted.coverage.counts[PHONE].roams == 1  # same day
    restarted.update([read("den", poll((PHONE, -50)))], back, TODAY)
    assert restarted.sightings[PHONE].arrived == T0
    # Only the first round after the restart stretches the gap.
    restarted.update([read("den", poll())], back + POLL, TODAY)
    away_long = back + POLL + GRACE + timedelta(minutes=5)
    restarted.update([read("den", poll((PHONE, -50)))], away_long, TODAY)
    assert restarted.sightings[PHONE].arrived == away_long


def test_after_an_unclean_stop_a_device_starts_a_new_visit() -> None:
    tracker = PresenceTracker(GRACE)
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    saved = tracker.to_storage(T0, clean=False)  # a periodic write, then a crash
    back = T0 + timedelta(hours=1)
    restarted = PresenceTracker(GRACE)
    restarted.restore(saved, back, TODAY)
    restarted.update([read("hall", poll((PHONE, -50)))], back, TODAY)
    assert restarted.sightings[PHONE].arrived == back


def test_restore_from_nothing_or_another_day() -> None:
    tracker = PresenceTracker(GRACE)
    tracker.restore(None, T0, TODAY)
    assert tracker.sightings == {} and tracker.coverage.counts == {}
    old = PresenceTracker(GRACE)
    old.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    old.update([read("den", poll((PHONE, -50)))], T0 + POLL, TODAY)
    tracker.restore(old.to_storage(T0 + POLL, clean=True), T0 + timedelta(days=1), TODAY + timedelta(days=1))
    assert PHONE in tracker.sightings  # within the 7-day retention
    assert tracker.coverage.counts == {}  # yesterday's counts


# --- poll_all ---


class Driver(AccessPointDriver):
    TYPE = "test_tracker"
    NAME = "Test"
    FIELDS = (DriverField("host"),)
    POLL_TIMEOUT = 0.05

    async def async_get_associated_clients(self):
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        behaviour = self.config["host"]
        if behaviour == "ok":
            return poll((PHONE, -50))
        if behaviour == "slow":
            await asyncio.sleep(1)
        raise AccessPointError("down")


def test_poll_all_returns_results_and_failures_in_order() -> None:
    results = asyncio.run(poll_all([Driver({"host": h}) for h in ("ok", "down", "slow")]))
    assert isinstance(results[0], PollResult)
    assert isinstance(results[1], AccessPointError)
    assert isinstance(results[2], TimeoutError)


# --- watch_forever ---


class Watching(Driver):
    WATCHES = True

    def __init__(self, config, outcomes) -> None:
        super().__init__(config)
        self.outcomes = list(outcomes)
        self.runs = 0

    async def async_watch(self, on_change) -> None:
        self.runs += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "forever"
        if outcome == "change":
            on_change()
            await asyncio.Event().wait()
        if outcome == "forever":
            await asyncio.Event().wait()
        if outcome == "bug":
            raise RuntimeError("driver bug")
        if outcome == "ends":
            return
        raise AccessPointError(outcome)


def run_watch(driver, **kwargs) -> list[str]:
    events: list[str] = []

    async def main():
        task = asyncio.ensure_future(
            watch_forever(
                driver,
                lambda: events.append("change"),
                on_paused=lambda reason: events.append(f"paused: {reason}"),
                on_resumed=lambda: events.append("resumed"),
                on_failed_again=lambda reason: events.append(f"again: {reason}"),
                retry_min=0,
                **kwargs,
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    return events


def test_watch_reports_changes_and_runs_until_cancelled() -> None:
    driver = Watching({"host": "x"}, ["change"])
    assert run_watch(driver) == ["change"]
    assert driver.runs == 1


def test_watch_retries_and_logs_a_pause_once() -> None:
    driver = Watching({"host": "x"}, ["lost", "lost again", "bug", "ends", "change"])
    assert run_watch(driver) == [
        "paused: lost",
        "again: lost again",
        "again: unexpected error",
        "again: the feed ended",
        "change",
    ]
    assert driver.runs == 5


def test_a_watch_that_ran_long_enough_resumes() -> None:
    driver = Watching({"host": "x"}, ["lost", "ends", "forever"])
    # stable=0: every run counts as having worked
    assert run_watch(driver, stable=0) == ["paused: lost", "resumed", "paused: the feed ended"]


# --- fast departure (opt-in) ---


def test_fast_departure_after_a_reported_leave() -> None:
    tracker = PresenceTracker(GRACE, fast_departure=timedelta(seconds=30))
    tracker.update([read("hall", poll((PHONE, -50), (LAPTOP, -60)))], T0, TODAY)
    leave = T0 + timedelta(seconds=20)
    tracker.note_leave("hall", leave)
    tracker.update([read("hall", poll((LAPTOP, -60)))], leave + timedelta(seconds=3), TODAY)
    # Before the fast departure time it's still home; after it, away (not after 3 min).
    assert tracker.current(PHONE, leave + timedelta(seconds=29)) is not None
    assert tracker.current(PHONE, leave + timedelta(seconds=30)) is None
    assert tracker.away_since(PHONE, leave + timedelta(seconds=40)) == leave + timedelta(seconds=30)
    # The laptop, seen in the round after the leave, isn't affected.
    assert tracker.current(LAPTOP, leave + timedelta(seconds=40)) is not None


def test_fast_departure_needs_a_round_of_that_ap_without_the_device() -> None:
    tracker = PresenceTracker(GRACE, fast_departure=timedelta(seconds=30))
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    leave = T0 + timedelta(seconds=10)
    tracker.note_leave("hall", leave)
    # No round since the leave: no evidence it was this device.
    assert tracker.current(PHONE, leave + timedelta(seconds=60)) is not None
    # Its access point couldn't be read in the next round: still no evidence.
    tracker.update([read("hall", AccessPointError("down")), read("den", poll())], leave + timedelta(seconds=5), TODAY)
    assert tracker.current(PHONE, leave + timedelta(seconds=60)) is not None
    # Seen again after the leave (a quick reconnect): it stays home.
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], leave + timedelta(seconds=8), TODAY)
    assert tracker.current(PHONE, leave + timedelta(seconds=60)) is not None


def test_fast_departure_is_off_by_default_and_has_a_floor() -> None:
    tracker = PresenceTracker(GRACE)
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    tracker.note_leave("hall", T0 + timedelta(seconds=5))
    tracker.update([read("hall", poll())], T0 + timedelta(seconds=8), TODAY)
    assert tracker.current(PHONE, T0 + timedelta(seconds=120)) is not None  # the grace period
    assert tracker.away_since(PHONE, T0) == T0 + GRACE
    assert PresenceTracker(GRACE, fast_departure=timedelta(seconds=1)).fast_departure == timedelta(seconds=10)


def test_watch_forever_passes_on_leave_only_when_given() -> None:
    calls: list[tuple] = []

    class Old(Driver):
        WATCHES = True

        async def async_watch(self, on_change):  # an older driver without on_leave
            calls.append(("old",))
            await asyncio.Event().wait()

    class New(Driver):
        WATCHES = True

        async def async_watch(self, on_change, on_leave=None):
            calls.append(("new", on_leave is not None))
            await asyncio.Event().wait()

    async def main() -> None:
        tasks = [
            asyncio.create_task(watch_forever(Old({"host": "x"}), lambda: None)),
            asyncio.create_task(watch_forever(New({"host": "x"}), lambda: None, on_leave=lambda: None)),
        ]
        await asyncio.sleep(0.01)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(main())
    assert sorted(calls) == [("new", True), ("old",)]


# --- review of 0.13.0: fast departure edge cases ---

FAST = timedelta(seconds=30)
S = timedelta(seconds=1)


def departed_tracker() -> tuple[PresenceTracker, datetime]:
    """PHONE on hall, a leave at T0+20, rounds without it: away at T0+50 (settled)."""
    tracker = PresenceTracker(GRACE, fast_departure=FAST)
    tracker.update([read("hall", poll((PHONE, -50)), ), read("den", poll())], T0, TODAY)
    leave = T0 + 20 * S
    tracker.note_leave("hall", leave)
    tracker.update([read("hall", poll()), read("den", poll())], leave + 3 * S, TODAY)
    tracker.update([read("hall", poll()), read("den", poll())], leave + 31 * S, TODAY)  # the deadline poll
    assert tracker.current(PHONE, leave + 31 * S) is None
    return tracker, leave


def test_a_settled_departure_survives_another_clients_leave() -> None:
    tracker, leave = departed_tracker()
    # Another client leaves the same access point; no round since: still away.
    tracker.note_leave("hall", leave + 40 * S)
    assert tracker.current(PHONE, leave + 41 * S) is None
    assert tracker.away_since(PHONE, leave + 41 * S) == leave + FAST


def test_a_settled_departure_survives_its_access_point_failing() -> None:
    tracker, leave = departed_tracker()
    tracker.update([read("hall", AccessPointError("down")), read("den", poll())], leave + 60 * S, TODAY)
    assert tracker.current(PHONE, leave + 61 * S) is None


def test_a_leave_during_a_slow_round_still_counts() -> None:
    tracker = PresenceTracker(GRACE, fast_departure=FAST)
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], T0, TODAY)
    # A round starts at T0+60, reads hall (PHONE still there), then PHONE leaves at
    # T0+62 while a slow access point holds the round up until T0+70.
    tracker.note_leave("hall", T0 + 62 * S)
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], T0 + 70 * S, TODAY,
                   started=T0 + 60 * S)
    tracker.update([read("hall", poll()), read("den", poll())], T0 + 95 * S, TODAY, started=T0 + 94 * S)
    # Away 30 s after the leave, not 3 minutes after the last sighting.
    assert tracker.current(PHONE, T0 + 95 * S) is None
    assert tracker.away_since(PHONE, T0 + 95 * S) == T0 + 92 * S


def test_a_return_after_a_fast_departure_is_a_new_visit() -> None:
    tracker, leave = departed_tracker()
    back = leave + 40 * S  # well within the original grace period
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], back, TODAY)
    sighting = tracker.current(PHONE, back)
    assert sighting is not None and sighting.arrived == back


def test_a_departure_due_between_rounds_is_settled_before_a_return() -> None:
    # No poll landed exactly at the deadline: the device is back in the next round.
    tracker = PresenceTracker(GRACE, fast_departure=FAST)
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    tracker.note_leave("hall", T0 + 10 * S)
    tracker.update([read("hall", poll())], T0 + 15 * S, TODAY)
    assert tracker.current(PHONE, T0 + 41 * S) is None  # away from T0+40
    tracker.update([read("hall", poll((PHONE, -50)))], T0 + 60 * S, TODAY)
    assert tracker.current(PHONE, T0 + 60 * S).arrived == T0 + 60 * S


def test_a_settled_departure_survives_a_restart() -> None:
    tracker, leave = departed_tracker()
    saved = tracker.to_storage(leave + 32 * S, clean=True)
    again = PresenceTracker(GRACE, fast_departure=FAST)
    again.restore(saved, leave + 40 * S, TODAY)
    assert again.current(PHONE, leave + 40 * S) is None


# --- second review: evidence is taken with its own round's state ---


def test_evidence_before_an_access_point_failure_still_counts() -> None:
    tracker = PresenceTracker(GRACE, fast_departure=FAST)
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], T0, TODAY)
    leave = T0 + 10 * S
    tracker.note_leave("hall", leave)
    # Evidence: hall read after the leave, PHONE missing.
    tracker.update([read("hall", poll()), read("den", poll())], leave + 5 * S, TODAY)
    # Then hall fails before the deadline: the evidence stands.
    tracker.update([read("hall", AccessPointError("down")), read("den", poll())], leave + 35 * S, TODAY)
    assert tracker.current(PHONE, leave + 35 * S) is None
    assert tracker.away_since(PHONE, leave + 35 * S) == leave + FAST


def test_a_recovering_access_point_is_no_evidence() -> None:
    tracker = PresenceTracker(GRACE, fast_departure=FAST)
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], T0, TODAY)
    leave = T0 + 10 * S
    tracker.note_leave("hall", leave)
    # hall can't be read after the leave: no evidence the phone left.
    tracker.update([read("hall", AccessPointError("down")), read("den", poll())], leave + 5 * S, TODAY)
    tracker.update([read("hall", AccessPointError("down")), read("den", poll())], leave + 40 * S, TODAY)
    assert tracker.current(PHONE, leave + 40 * S) is not None
    # hall recovers with the phone on it: the same visit goes on, nothing invented.
    tracker.update([read("hall", poll((PHONE, -50))), read("den", poll())], leave + 60 * S, TODAY)
    assert tracker.current(PHONE, leave + 60 * S).arrived == T0


def test_a_device_back_before_the_deadline_never_left() -> None:
    tracker = PresenceTracker(GRACE, fast_departure=FAST)
    tracker.update([read("hall", poll((PHONE, -50)))], T0, TODAY)
    tracker.note_leave("hall", T0 + 10 * S)
    tracker.update([read("hall", poll())], T0 + 15 * S, TODAY)  # evidence, deadline T0+40
    tracker.update([read("hall", poll((PHONE, -50)))], T0 + 25 * S, TODAY)  # back in time
    assert tracker.current(PHONE, T0 + 60 * S).arrived == T0
