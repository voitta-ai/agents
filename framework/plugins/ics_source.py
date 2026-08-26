"""ICS calendar source (issue #22).

Reads a calendar's ICS feed and emits Signals for events that fall on
*tomorrow*, split by commitment:

  - **must** — a personal commitment: the owner created the event, or has
    personally accepted an invite to it. This is a reminder, not a suggestion.
  - **may**  — a group/optional event the owner has not (yet) accepted. This
    is a suggestion.

The split is the entire point of the feature, so it is conservative by
design: an event the owner has already personally accepted can never come
out as "may", and an event whose owner-response cannot be determined at all
defaults to "may", never "must" (see `_classify`). A declined event is
dropped outright — the owner already said no, it should not haunt them as a
suggestion either.

This module only builds Signals; rendering the two named sections ("YOU MUST
ATTEND" / "YOU MAY CONSIDER ATTENDING") is a downstream scorer/destination
concern (`meta.commitment` and `meta.reason` carry what it needs).

## Transport

The feed URL is a bearer secret (Google Calendar's private ICS URL grants
read access to the whole calendar to anyone who has it), so it is read from
an environment variable named by `url_env` (default `ICS_CALENDAR_URL`) and
never accepted from config, matching the `webhook_env`/`api_key_env`
convention the Slack destination and AgentMail source already use.

**Never swallow a failed fetch** (see reddit_source.py's `feed_errors`
lesson from PR #23): `_fetch_bytes` calls `raise_for_status()` and nothing
here catches the resulting exception. A broken feed must break the run.

## Timezone — the thing ICS parsing gets wrong

"Tomorrow" is meaningless without a reference timezone, and the machine
running this agent (cron/launchd, likely UTC) is not the owner's timezone.
So `timezone` (an IANA name, e.g. `America/Los_Angeles`) is a *required*
config key — there is no default, because silently defaulting to UTC would
misfile events near midnight without ever raising an error.

Every datetime this module produces or compares — the "tomorrow" window,
each event's start/end, RRULE occurrences, EXDATE/RECURRENCE-ID instants —
is normalized into that one configured zone via `_to_owner_local`/
`_occurrence_instant`, regardless of what TZID (if any) the event itself
declares. An event declared in `America/New_York` and an owner configured
for `America/Los_Angeles` must land on the correct owner-local day and show
owner-local clock times; the fixture's "tz-not-UTC" case exists to prove
that conversion, not just that parsing doesn't crash.

All-day events (`DTSTART;VALUE=DATE`) have no timezone at all by design —
they are compared as dates, not instants.

## Recurrence (RRULE / EXDATE / RECURRENCE-ID) — handled, not dropped

A recurring event with no per-instance overrides (e.g. a daily standup) is
expanded with `dateutil.rrule` and windowed to tomorrow. `EXDATE` entries
(a single cancelled instance) are excluded. A modified instance — a second
VEVENT sharing the series' UID and carrying `RECURRENCE-ID` — is used in
place of whatever the base rule would have generated for that original
slot, so a moved-30-minutes-later standup shows once, at its new time, not
twice. An instance whose override carries `STATUS:CANCELLED` is dropped
entirely, like a decline.

**Not handled, deliberately, and flagged rather than silently mishandled:**

  - `RDATE` (ad hoc extra dates bolted onto a rule) is not read.
  - A *recurring all-day* series (`VALUE=DATE` + `RRULE` on the same VEVENT)
    is not expanded — only its literal `DTSTART` is considered. Recurring
    meetings are the common case this exists for; recurring all-day series
    (e.g. an annual holiday marked as a yearly rule rather than one event
    per year) are rare enough, and date-only recurrence math different
    enough from datetime recurrence, to cut for this PR rather than rush.

Both are real gaps for a feed that uses them — noted here so they read as a
scoping decision, not a bug someone finds by accident.

## Dedupe id

`Signal.id` is `f"{uid}:{start_iso}"` — the ICS UID (stable across edits to
an event, per RFC 5545: editing a summary/time bumps SEQUENCE/DTSTAMP, not
UID) plus the *occurrence's own* start. That makes the id stable when an
event is edited without moving (an edited title still dedupes as "seen"),
while still giving every RRULE instance and every RECURRENCE-ID override its
own id (next week's standup is a different id from this week's, so it is not
silently swallowed by `SeenStore` treating last week's occurrence as
"already seen" forever). A rescheduled event gets a new id and is signalled
again — treated as a fresh notification for the new slot, which is the
correct behaviour for a reminder/suggestion digest.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import icalendar
import requests
from dateutil.rrule import rrulestr

from framework.interfaces import Signal, Source
from framework.registry import register_source

_DEFAULT_LABEL = "ics-calendar"

# PARTSTAT values that mean "not attending" -> the event is dropped
# entirely, never resurfaced as a suggestion.
_DECLINED_PARTSTATS = {"DECLINED"}
# PARTSTAT values that promote an invite to MUST ATTEND. Everything else
# (NEEDS-ACTION, TENTATIVE, or PARTSTAT simply absent) is the safe default:
# MAY CONSIDER. Wrongly promoting an unanswered invite to MUST is the worse
# error, so absence never defaults to MUST.
_ACCEPTED_PARTSTATS = {"ACCEPTED"}


def _mailto(value: object) -> str:
    """ORGANIZER/ATTENDEE values are `mailto:` URIs; normalize to a bare,
    lowercased email so they can be compared against the configured owner
    email regardless of case or the scheme prefix."""
    text = str(value or "")
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:"):]
    retval = text.strip().lower()
    return retval


def _to_owner_local(value: datetime, owner_tz: ZoneInfo) -> datetime:
    """Normalize a datetime — aware in its own TZID, aware in UTC, or a bare
    floating-time value with no tzinfo at all — into the configured owner
    timezone. Every windowing decision and every displayed clock time goes
    through this, never through an event's own declared zone."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=owner_tz)
    retval = aware.astimezone(owner_tz)
    return retval


def _occurrence_instant(value: object, owner_tz: ZoneInfo) -> datetime:
    """Normalize an EXDATE or RECURRENCE-ID value (date or datetime) to a
    UTC instant, so it can be compared by absolute moment against an
    RRULE-generated occurrence regardless of which tzinfo object each one
    happens to carry."""
    if isinstance(value, datetime):
        aware = _to_owner_local(value, owner_tz)
    else:
        aware = datetime.combine(value, time.min, tzinfo=owner_tz)
    retval = aware.astimezone(dt_timezone.utc)
    return retval


def _is_cancelled(component: icalendar.Event) -> bool:
    retval = str(component.get("status", "")).upper() == "CANCELLED"
    return retval


def _event_span(
    component: icalendar.Event, owner_tz: ZoneInfo
) -> tuple[datetime | date, datetime | date, bool]:
    """(start, end, all_day) for a single (non-expanded) VEVENT, in owner-local
    time for timed events, or as bare dates for all-day events."""
    dtstart_prop = component.get("dtstart")
    dtend_prop = component.get("dtend")
    start_val = dtstart_prop.dt

    if isinstance(start_val, datetime):
        start = _to_owner_local(start_val, owner_tz)
        if dtend_prop is not None:
            end = _to_owner_local(dtend_prop.dt, owner_tz)
        else:
            duration_prop = component.get("duration")
            delta = duration_prop.dt if duration_prop is not None else timedelta(hours=1)
            end = start + delta
        retval = (start, end, False)
    else:
        # VALUE=DATE: no timezone concept. DTEND is exclusive per RFC 5545.
        end_val = dtend_prop.dt if dtend_prop is not None else start_val + timedelta(days=1)
        retval = (start_val, end_val, True)
    return retval


def _all_day_covers(start_date: date, end_date_exclusive: date, day: date) -> bool:
    retval = start_date <= day < end_date_exclusive
    return retval


def _timed_overlaps(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> bool:
    """Whether a timed event intersects the window at all.

    Deliberately overlap rather than start-in-window: an event that begins today
    and runs past midnight is on tomorrow's calendar, and silently dropping it is
    the exact failure this agent exists to prevent. Half-open on both sides, so
    an event ending exactly at midnight does not count as tomorrow's."""
    retval = start < window_end and end > window_start
    return retval


def _exdate_instants(component: icalendar.Event, owner_tz: ZoneInfo) -> set[datetime]:
    prop = component.get("exdate")
    if prop is None:
        return set()
    entries = prop if isinstance(prop, list) else [prop]
    retval: set[datetime] = set()
    for entry in entries:
        for wrapper in entry.dts:
            retval.add(_occurrence_instant(wrapper.dt, owner_tz))
    return retval


def _expand_master(
    component: icalendar.Event,
    owner_tz: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
    skip_instants: set[datetime],
) -> list[tuple[datetime, datetime]]:
    """RRULE occurrences of one (timed, non-all-day) master falling in the
    tomorrow window, minus EXDATE entries and minus any instant a
    RECURRENCE-ID override already covers (`skip_instants`)."""
    dtstart_prop = component.get("dtstart")
    start_val = dtstart_prop.dt
    dtend_prop = component.get("dtend")
    if dtend_prop is not None:
        duration = dtend_prop.dt - start_val
    else:
        duration_prop = component.get("duration")
        duration = duration_prop.dt if duration_prop is not None else timedelta(hours=1)

    # Expand in the event's own declared zone (or owner zone, for a floating
    # value) — RRULE stepping (e.g. "daily at 9am") is only correct relative
    # to whichever zone the series' wall-clock time is actually anchored to.
    native_dtstart = start_val if start_val.tzinfo is not None else start_val.replace(tzinfo=owner_tz)
    rule = rrulestr(component.get("rrule").to_ical().decode(), dtstart=native_dtstart)
    exdates = _exdate_instants(component, owner_tz)

    occurrences: list[tuple[datetime, datetime]] = []
    # Search back by one duration: an occurrence that starts before the window
    # but runs into it overlaps tomorrow and must be reported.
    search_start = window_start - duration
    for occ_start_native in rule.between(search_start, window_end, inc=True):
        occ_start = occ_start_native.astimezone(owner_tz)
        if not _timed_overlaps(occ_start, occ_start + duration, window_start, window_end):
            continue
        instant = occ_start.astimezone(dt_timezone.utc)
        if instant in exdates or instant in skip_instants:
            continue
        occurrences.append((occ_start, occ_start + duration))
    return occurrences


def _expand_calendar(
    cal: icalendar.Calendar, owner_tz: ZoneInfo, window_start: datetime, window_end: datetime
) -> list[tuple[icalendar.Event, datetime | date, datetime | date, bool]]:
    """Every occurrence — single events, RRULE expansions, and
    RECURRENCE-ID overrides substituted in for the base occurrence they
    replace — that falls on tomorrow, as (component, start, end, all_day)."""
    tomorrow = window_start.date()
    by_uid: dict[str, list[icalendar.Event]] = {}
    for component in cal.walk("VEVENT"):
        by_uid.setdefault(str(component.get("uid", "")), []).append(component)

    results: list[tuple[icalendar.Event, datetime | date, datetime | date, bool]] = []
    for components in by_uid.values():
        masters = [c for c in components if c.get("recurrence-id") is None]
        overrides = [c for c in components if c.get("recurrence-id") is not None]

        skip_instants: set[datetime] = set()
        for override in overrides:
            skip_instants.add(_occurrence_instant(override.get("recurrence-id").dt, owner_tz))
            if _is_cancelled(override):
                continue  # this one instance was cancelled: drop it, like a decline
            start, end, all_day = _event_span(override, owner_tz)
            if all_day:
                if _all_day_covers(start, end, tomorrow):
                    results.append((override, start, end, all_day))
            elif _timed_overlaps(start, end, window_start, window_end):
                results.append((override, start, end, all_day))

        for master in masters:
            if master.get("dtstart") is None or _is_cancelled(master):
                continue
            start_val = master.get("dtstart").dt
            if isinstance(start_val, datetime) and master.get("rrule") is not None:
                for occ_start, occ_end in _expand_master(
                    master, owner_tz, window_start, window_end, skip_instants
                ):
                    results.append((master, occ_start, occ_end, False))
            else:
                start, end, all_day = _event_span(master, owner_tz)
                if all_day:
                    if _all_day_covers(start, end, tomorrow):
                        results.append((master, start, end, all_day))
                elif _timed_overlaps(start, end, window_start, window_end):
                    results.append((master, start, end, all_day))
    return results


def _attendees(component: icalendar.Event) -> list[tuple[str, str]]:
    raw = component.get("attendee")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    retval = [(_mailto(item), str(item.params.get("PARTSTAT", "")).upper()) for item in items]
    return retval


def _organizer(component: icalendar.Event) -> str:
    raw = component.get("organizer")
    retval = _mailto(raw) if raw is not None else ""
    return retval


def _classify(
    owner_email: str, organizer_email: str, attendees: list[tuple[str, str]]
) -> tuple[str, str] | None:
    """(commitment, reason), or None if the event should be dropped
    (declined). See the module docstring for the ordering rationale.

      1. No ATTENDEE at all -> a personal calendar entry, not an invite. No
         one else's response to weigh, so it is the owner's own commitment
         by construction.
      2. Owner is the ORGANIZER -> they created it -> must, no matter what
         (or whether) the attendee list says.
      3. Owner appears as an ATTENDEE -> follow their own PARTSTAT.
      4. Owner does not appear among attendees at all (owner_email does not
         match how this calendar lists itself, or hides behind a
         distribution list) -> may. Same reasoning as PARTSTAT-absent: never
         guess "must".
    """
    if not attendees:
        retval = ("must", "Personal calendar entry.")
        return retval
    if owner_email and organizer_email == owner_email:
        retval = ("must", "You organized it.")
        return retval
    for email, partstat in attendees:
        if email == owner_email:
            if partstat in _ACCEPTED_PARTSTATS:
                retval = ("must", "You accepted.")
                return retval
            if partstat in _DECLINED_PARTSTATS:
                retval = None
                return retval
            retval = ("may", "Group event, optional, you have not responded.")
            return retval
    retval = ("may", "Group event, optional; your response status could not be determined.")
    return retval


class ICSSource:
    def __init__(
        self,
        url: str,
        owner_email: str,
        tz: ZoneInfo,
        label: str = _DEFAULT_LABEL,
        timeout: int = 30,
        now: datetime | None = None,
    ):
        self.url = url
        self.owner_email = owner_email.strip().lower()
        self.tz = tz
        self.label = label
        self.timeout = timeout
        # Injectable reference "now", used only by the fixture demo so it can
        # pin "tomorrow" to a fixed date. `build()` never sets this — real
        # runs always use the real current time.
        self._now = now

    def _fetch_bytes(self) -> bytes:
        resp = requests.get(self.url, timeout=self.timeout)
        resp.raise_for_status()  # never swallow a failed fetch
        retval = resp.content
        return retval

    def _tomorrow_window(self) -> tuple[datetime, datetime]:
        now_local = self._now.astimezone(self.tz) if self._now is not None else datetime.now(self.tz)
        tomorrow = now_local.date() + timedelta(days=1)
        start = datetime.combine(tomorrow, time.min, tzinfo=self.tz)
        retval = (start, start + timedelta(days=1))
        return retval

    def parse(self, raw: bytes) -> list[Signal]:
        """Split out from `fetch()` so it can be exercised directly against a
        fixture file, with no network involved."""
        try:
            cal = icalendar.Calendar.from_ical(raw)
        except Exception as exc:
            raise RuntimeError(
                f"ICS feed at {self.url} did not parse as a calendar: {exc}. "
                "Treating this as a hard failure rather than an empty day - a "
                "feed that stops being a calendar must not read as 'nothing "
                "scheduled tomorrow'."
            ) from exc

        # An empty day is legitimate; a feed carrying no VEVENT at all is not.
        # Distinguishing them is the whole point: the repo has already been
        # bitten twice by a broken source that looked like a quiet one.
        if not list(cal.walk("VEVENT")):
            raise RuntimeError(
                f"ICS feed at {self.url} contains no VEVENT components. "
                "A calendar with zero events is almost always a broken or "
                "wrong URL, an auth redirect, or an HTML error page - not a "
                "genuinely empty calendar. Verify the URL before assuming "
                "there is nothing scheduled."
            )

        window_start, window_end = self._tomorrow_window()

        signals: list[Signal] = []
        for component, start, end, all_day in _expand_calendar(cal, self.tz, window_start, window_end):
            organizer_email = _organizer(component)
            attendees = _attendees(component)
            classification = _classify(self.owner_email, organizer_email, attendees)
            if classification is None:
                continue
            commitment, reason = classification

            uid = str(component.get("uid", ""))
            summary = str(component.get("summary", "")).strip() or "(no title)"
            time_label = "All day" if all_day else f"{start:%H:%M}-{end:%H:%M}"
            text = f"{time_label} — {summary}. {reason}"
            if attendees:
                text += f" {len(attendees)} attendee(s)."

            signals.append(
                Signal(
                    id=f"{uid}:{start.isoformat()}",
                    text=text,
                    url=str(component.get("url", "") or ""),
                    source=self.label,
                    meta={
                        "uid": uid,
                        "title": summary,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "all_day": all_day,
                        "commitment": commitment,  # "must" | "may"
                        "reason": reason,
                        "organizer": organizer_email,
                        "is_organizer": bool(self.owner_email) and organizer_email == self.owner_email,
                        "attendee_count": len(attendees),
                        "location": str(component.get("location", "") or ""),
                    },
                )
            )

        retval = signals
        return retval

    def fetch(self) -> list[Signal]:
        raw = self._fetch_bytes()
        retval = self.parse(raw)
        return retval


@register_source("ics")
def build(cfg: dict) -> Source:
    url_env = cfg.get("url_env", "ICS_CALENDAR_URL")
    url = os.environ.get(url_env)
    if not url:
        raise RuntimeError(
            f"ics source needs the feed URL in ${url_env} — it is a bearer secret "
            "(Google's private ICS URL grants read access to the whole calendar), "
            "so it is never accepted from config"
        )

    owner_email = cfg.get("owner_email") or os.environ.get("CALENDAR_OWNER_EMAIL")
    if not owner_email:
        raise RuntimeError(
            "ics source needs 'owner_email' in config or CALENDAR_OWNER_EMAIL in env, to tell "
            "MUST ATTEND apart from MAY CONSIDER — without it every invite would have to default "
            "to MAY, which defeats the point of the split"
        )

    tz_name = cfg.get("timezone")
    if not tz_name:
        raise RuntimeError(
            "ics source needs 'timezone' (an IANA name, e.g. 'America/Los_Angeles') — "
            "'tomorrow' is evaluated in this zone, never UTC or the host machine's zone"
        )
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise RuntimeError(f"ics source: invalid 'timezone' {tz_name!r}: {exc}") from exc

    retval = ICSSource(
        url=url,
        owner_email=owner_email,
        tz=tz,
        label=cfg.get("label", _DEFAULT_LABEL),
        timeout=int(cfg.get("timeout", 30)),
    )
    return retval
