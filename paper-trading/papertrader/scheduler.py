"""Paper-only scheduler: runs `scan` on a fixed interval and sends a daily leaderboard.

This is the only automation in the project and it can only ever drive the PAPER broker:
the paper-mode check is repeated before every single run, so switching the config to
anything else stops the scheduler instead of trading.
"""
import fcntl
import logging
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .safety import LiveTradingLocked, assert_paper_mode

log = logging.getLogger(__name__)


def next_run(now: datetime, interval_minutes: int, offset_seconds: int) -> datetime:
    """Next slot aligned to the interval (e.g. hh:00 for 60 min) plus an offset, so the
    scan starts shortly after candles close and the data provider has published them."""
    step = timedelta(minutes=interval_minutes)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slot = midnight + ((now - midnight) // step) * step + timedelta(seconds=offset_seconds)
    while slot <= now:
        slot += step
    return slot


def report_due(now: datetime, report_time_utc: str | None, last_report_date: str | None) -> bool:
    if not report_time_utc:
        return False
    hh, mm = (int(x) for x in report_time_utc.split(":"))
    return now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0) and last_report_date != now.date().isoformat()


@contextmanager
def scan_lock(db_path: str):
    """Stop two scans (scheduler + a manual run) from writing the journal at once.
    Yields False if another scan holds the lock."""
    lock_path = Path(db_path).with_suffix(".lock") if db_path != ":memory:" else Path("scan.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class Scheduler:
    def __init__(self, load_config, scan, send_report, notify, get_state, set_state,
                 clock=lambda: datetime.now(timezone.utc), sleep=time.sleep):
        self.load_config = load_config  # re-read every run so config edits apply without a restart
        self.scan = scan
        self.send_report = send_report
        self.notify = notify
        self.get_state, self.set_state = get_state, set_state
        self.clock, self.sleep = clock, sleep
        self.stopping = False

    def stop(self, *_):
        log.info("stop requested, finishing current step")
        self.stopping = True

    def _sleep_until(self, when: datetime):
        while not self.stopping:
            remaining = (when - self.clock()).total_seconds()
            if remaining <= 0:
                return
            self.sleep(min(remaining, 30))

    def run(self, max_runs: int | None = None, run_now: bool = True):
        cfg = self.load_config()
        sched = cfg.get("schedule", {})
        self.notify(f"🧪 Paper scheduler started · scan every {sched.get('interval_minutes', 60)} min · "
                    "PAPER ONLY (live trading locked)")
        if not run_now:
            self._sleep_until(next_run(self.clock(), sched.get("interval_minutes", 60), sched.get("offset_seconds", 120)))
        runs, failures = 0, 0
        while not self.stopping and (max_runs is None or runs < max_runs):
            try:
                cfg = self.load_config()  # re-checks paper mode every run
                assert_paper_mode(cfg)
                sched = cfg.get("schedule", {})
                self.scan(cfg)
                failures = 0
                now = self.clock()
                if report_due(now, sched.get("daily_report_utc", "21:00"), self.get_state("last_daily_report")):
                    self.send_report(cfg)
                    self.set_state("last_daily_report", now.date().isoformat())
            except LiveTradingLocked as exc:
                self.notify(f"⛔ Scheduler stopped: {exc}")
                raise
            except Exception as exc:
                failures += 1
                log.exception("scheduled scan failed")
                if failures in (1, 3) or failures % 24 == 0:  # don't spam during a long outage
                    self.notify(f"⚠️ Scheduled paper scan failed ({failures}x in a row): {exc}")
            runs += 1
            if max_runs is not None and runs >= max_runs:
                break
            self._sleep_until(next_run(self.clock(), int(sched.get("interval_minutes", 60)),
                                       int(sched.get("offset_seconds", 120))))
        self.notify("🧪 Paper scheduler stopped")


def install_signal_handlers(scheduler: Scheduler):
    signal.signal(signal.SIGTERM, scheduler.stop)
    signal.signal(signal.SIGINT, scheduler.stop)
