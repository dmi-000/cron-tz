#!/usr/bin/env python3
"""
adjust_cron_tz.py — Crontab compiler with timezone and solar time support.

Source of truth: ~/.crontab_src  (edit this, not crontab directly)
Output: overwrites crontab via `crontab -`

Run on wake, on network change, or daily to keep cron times current when
the system timezone changes or to update solar times seasonally.

Source file format
------------------
Mostly looks like a crontab.  The first field of each line determines how
it is treated — no other context matters:

  HH:MM               → tz-aware fixed-time job (requires active # tz: directive)
  HH:MM~N             → same, with up to N minutes of random jitter
  solar_event[±M][~N] → tz-aware solar job      (requires # tz: and # lat/lon)
  anything else       → passed through to crontab unchanged (digit, *, @, KEY=, #)

The HH and MM parts of a fixed-time spec accept the same range, list, and
step syntax that crontab uses in its minute and hour fields:

  09:00,30     * * *  cmd        → two jobs (09:00 and 09:30)
  9,17:00      * * *  cmd        → two jobs (09:00 and 17:00)
  9-17/2:00    * * *  cmd        → jobs at 09:00, 11:00, 13:00, 15:00, 17:00
  */4:00       * * *  cmd        → jobs at 00:00, 04:00, 08:00 … 20:00

Each expanded time is converted through the timezone separately and emits
its own cron line.

Special day-of-month values
---------------------------
The dom field (first of the three day/month/dow fields) may use two
extensions that standard crontab does not support:

  Negative offsets from end of month:

    18:00  -1  *  *    cmd   → last day of month
    18:00  -2  *  *    cmd   → second-to-last day
    18:00  -7  *  *    cmd   → seventh-to-last day

  Ordinal weekday (dom = ordinal, dow = weekday name or 0–6):

    09:00  first   *  monday   cmd   → first Monday of month
    09:00  second  *  tuesday  cmd   → second Tuesday
    09:00  third   *  Wed      cmd   → third Wednesday
    09:00  fourth  *  4        cmd   → fourth Thursday (cron dow 4)
    09:00  last    *  friday   cmd   → last Friday of month

  first through fourth use the dom-range trick (no shell guard needed).
  last, -1, -2, … wrap the command in a python3 guard.

Interval-firing jobs:

  HH:MM/Nm              → fire every N minutes, starting from HH:MM today
  HH:MM/Nm~J            → same, with up to J minutes of random jitter per firing
  HH:MM/Nd              → fire at HH:MM every N days (one firing per matching day)
  HH:MM/Nd~J            → same, with up to J minutes of jitter

  Examples:
    02:00/890m~5            * * *  python3 ~/e/src/fetch_enphase_forecast.py
    2026-06-24T02:00/890m~5 * * *  python3 ~/e/src/fetch_enphase_forecast.py
    06:00/5d                * * *  python3 ~/bin/weekly_report.py

  /Nm: at compile time the script walks forward from the epoch in N-minute
  steps and emits one cron line per firing that falls within the calendar day.
  The epoch is the date-prefixed form if present (YYYY-MM-DDTHH:MM), else the
  active # epoch: directive, else HH:MM today.  With a fixed epoch the firing
  times drift by (N mod 1440) minutes per day across recompiles — useful for
  irrational-interval sampling (890 min ≈ 1/φ days).  Without a fixed epoch
  the times are identical on every recompile.

  /Nd: equivalent to a fixed-time job with an every_n_days filter.  The
  reference date for the cycle comes from the date-prefixed form, the # epoch:
  directive, or today (fires on day 0, N, 2N, … from that date).

  For /Nm, the dom/month/dow fields are passed through unchanged and the
  active # filter: directive is applied on top.  For /Nd, the every_n_days
  cycle replaces the active # filter: — combine manually if both are needed.

Solar events (all require # lat/lon directives):

  sunrise            → standard sunrise (sun at geometric horizon)
  sunset             → standard sunset
  civil_dawn         → sun 6° below horizon (start of civil twilight)
  civil_dusk         → sun 6° below horizon (end of civil twilight)
  nautical_dawn      → sun 12° below horizon
  nautical_dusk      → sun 12° below horizon
  astronomical_dawn  → sun 18° below horizon (true dark sky)
  astronomical_dusk  → sun 18° below horizon
  solarnoon          → solar noon (sun at highest point)

All solar events accept an optional ±M minute offset and ~N jitter suffix:

  civil_dawn-30~5 * * * python3 ~/bin/photography.py

Standard 5-field cron lines are always raw, even inside a # tz: section.
No closing directive is needed; # tz: only affects lines whose first field
is HH:MM or a solar event name.

Three directive types extend plain crontab:

1. Timezone/location directives in comments:

     # tz: Europe/London
     # tz: local          ← use whatever timezone the system is currently in
     # lat: 51.50  lon: -0.12

   Apply to subsequent tz-aware jobs until overridden by another directive.
   'local' re-resolves the system timezone on each compile run, so the
   crontab stays correct if the machine moves or DST changes.

2. Fixed-time and solar jobs (HH:MM or solar event replaces min+hour):

     06:00~5        * * *  python3 ~/bin/morning_job.py
     sunset+30      * * *  python3 ~/bin/evening_job.py
     astronomical_dusk * * * python3 ~/bin/astronomy.py

   Compiled to today's time; re-run this script daily to keep solar times current.

3. Day filter directive:

     # filter: workday

   Restricts which days subsequent tz-aware jobs run.  Scope is sticky —
   applies to all following jobs until overridden by another # filter: or
   cleared with # filter: none.  Options:

     workday                              Mon–Fri (modifies crontab dow field)
     weekend                              Sat–Sun (modifies crontab dow field)
     last_dom                             last day of each month
     nth_weekday:N,DOW                    Nth weekday of month (e.g. 2,Mon)
     every_n_days:N,YYYY-MM-DD           every N days from reference date
     between:YYYY-MM-DD,YYYY-MM-DD       date-range gate
     none  (or: off)                      clear filter (runs every day)

   workday/weekend modify the crontab dow field directly.  The others wrap
   the command in a python3 one-liner guard (requires python3 in PATH at
   job runtime).  Filtered jobs exit 0 silently on non-matching days.

   The reference date in every_n_days is the phase: change it to shift
   which days the cycle lands on.

   Note: workday/weekend/nth_weekday filters conflict with ordinal-weekday
   dom specs (first/last/…) and will be ignored for those jobs with a
   warning comment.

Jitter
------
Appending ~N to a time spec adds a random delay of 0–N minutes at
compile time.  A new random offset is chosen on each recompile.

     08:00~10  * * * python3 ~/bin/job.py   # fires 08:00–08:10

For solar range specs, ~N can appear on an endpoint (jitters the range
boundary once, then all firings march uniformly) or at the end of the
whole spec (jitters each firing independently), or both:

     sunrise-30~60-sunset+75/1h    # start shifts by 0–60 min; step uniform
     sunrise-30-sunset+75/1h~60    # start fixed; each firing shifts 0–60 min
     sunrise-30~60-sunset+75/1h~5  # both: boundary jitter + per-firing jitter

Day-of-week adjustment
----------------------
When a time conversion crosses midnight (e.g. 23:30 New_York on a UTC
system becomes 04:30 UTC the next day), the compiler shifts the dow field
by ±1 day automatically for simple expressions (single values, ranges,
comma lists of numbers or 3-letter abbreviations).  A warning comment is
emitted for step expressions or other complex forms.

Round-tripping
--------------
The generated crontab embeds each intended-time line as a comment:

     # tz: Europe/London
     # lat: 51.50  lon: -0.12
     # filter: workday
     # [tz-src] 06:00 * * * python3 ~/bin/morning_job.py
     0 6 * * 1-5 python3 ~/bin/morning_job.py

When the generated crontab is used as src, [tz-src] lines are parsed as
the job source and the compiled line immediately following is skipped and
replaced.  Raw lines added directly to the crontab have no preceding
[tz-src] comment and pass through untouched.

Example ~/.crontab_src
----------------------
  SHELL=/bin/bash
  PATH=/usr/local/bin:/usr/bin:/bin

  # recompile daily at 03:00 UTC to update solar times
  0 3 * * * python3 ~/bin/adjust_cron_tz.py

  # tz: Europe/London
  # lat: 51.50  lon: -0.12
  # filter: workday
  06:00~5        * * *   python3 ~/bin/morning_job.py

  # filter: none
  18:00  last  *  friday  python3 ~/bin/end_of_month_review.py
  civil_dusk+15  * * *   python3 ~/bin/lights_on.py
  sunset+30      * * *   python3 ~/bin/evening_job.py

  # UTC — raw lines pass through unchanged
  0 4 * * 0 /usr/bin/weekly-backup.sh
"""

import argparse
import fcntl
import math
import os
import random
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

META = Path("~/.crontab_src").expanduser()

# First-field pattern for tz-aware fixed-time jobs.
# HH and MM may be crontab field expressions (digits, *, , - /).
_TIME_RE = re.compile(r"^([\d*,/\-]+):([\d*,/\-]+)(?:~(\d+))?$")

# Solar event names and their horizon elevation angles (degrees)
_SOLAR_ELEVATION: dict[str, float] = {
    "sunrise":             -0.833,
    "sunset":              -0.833,
    "civil_dawn":          -6.0,
    "civil_dusk":          -6.0,
    "nautical_dawn":      -12.0,
    "nautical_dusk":      -12.0,
    "astronomical_dawn":  -18.0,
    "astronomical_dusk":  -18.0,
}
_SOLAR_RISE_EVENTS = frozenset(
    {"sunrise", "civil_dawn", "nautical_dawn", "astronomical_dawn"}
)
_SOLAR_EVENT_NAMES = [
    "astronomical_dawn", "astronomical_dusk",
    "nautical_dawn", "nautical_dusk",
    "civil_dawn", "civil_dusk",
    "solarnoon",
    "sunrise", "sunset",
]
_ALL_SOLAR_EVENTS = frozenset(_SOLAR_EVENT_NAMES)

_SOLAR_RE = re.compile(
    r"^(" + "|".join(_SOLAR_EVENT_NAMES) + r")([+-]\d+)?(?:~(\d+))?$"
)

# Solar range: event1[±off][~jitter1]-event2[±off][~jitter2]/N(m|h)[~jitter]
# Step must carry a unit (m=minutes, h=hours) to avoid ambiguity with crontab step syntax.
# ~J on an endpoint jitters the range boundary once at compile time.
# ~J at the end jitters each individual firing independently.
# Examples: civil_dawn-civil_dusk/1h  sunrise-30~60-sunset+75/1h  sunrise-sunset/30m~5
_SOLAR_RANGE_RE = re.compile(
    r"^(" + "|".join(_SOLAR_EVENT_NAMES) + r")([+-]\d+)?(?:~(\d+))?"
    r"-(" + "|".join(_SOLAR_EVENT_NAMES) + r")([+-]\d+)?(?:~(\d+))?"
    r"/(\d+)(m|h)"
    r"(?:~(\d+))?$"
)

# Directive patterns (searched anywhere in a comment line)
_TZ_RE     = re.compile(r"\btz\s*:\s*(\S+)")
_LAT_RE    = re.compile(r"\blat\s*:\s*(-?\d+(?:\.\d*)?)")
_LON_RE    = re.compile(r"\blon\s*:\s*(-?\d+(?:\.\d*)?)")
_FILTER_RE = re.compile(r"\bfilter\s*:\s*(\S+)")
_EPOCH_RE  = re.compile(r"\bepoch\s*:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})")
_TZ_SRC_RE = re.compile(r"^#\s*\[tz-src\]\s+(.+)$")

# [YYYY-MM-DDT]HH:MM/Nm[~jitter] and .../Nd[~jitter] — interval time specs.
# The optional date prefix embeds the epoch inline; without it the epoch is
# HH:MM today (same times each day) or the active # epoch: directive.
# The unit suffix (m/d) distinguishes from the crontab step syntax that
# _TIME_RE handles (e.g. '9-17:00/2' is a valid crontab step expression).
_TIME_INTERVAL_RE = re.compile(
    r"^(?:(\d{4}-\d{2}-\d{2})T)?(\d{1,2}):(\d{2})/(\d+)(m|d)(?:~(\d+))?$"
)

# Heuristic to catch '# filter keyword' with a missing colon
_FILTER_TYPO_RE = re.compile(
    r"\bfilter\s+(?:workday|weekend|last_dom|nth_weekday|every_n_days|between|none|off)\b"
)

# Crontab field patterns.
# min/hour fields accept only numeric expressions (no names); dom/month/dow allow names.
_CRON_NUM_FIELD_RE  = re.compile(r'^[0-9*,/\-]+$')
_CRON_FIELD_RE      = re.compile(r'^[0-9a-zA-Z*,/\-]+$')
_CRON_SPECIALS = frozenset([
    "@reboot", "@yearly", "@annually", "@monthly",
    "@weekly", "@daily", "@midnight", "@hourly",
])


def _is_valid_crontab_line(parts: list[str]) -> bool:
    """Return True if parts looks like a valid standard crontab line."""
    if not parts:
        return False
    if parts[0].startswith("@"):
        return parts[0].lower() in _CRON_SPECIALS and len(parts) >= 2
    if len(parts) < 6:
        return False
    # min, hour: digits/*,/- only (no names — catches e.g. 'sunrise-sunset/2' as minute)
    if not (_CRON_NUM_FIELD_RE.match(parts[0]) and _CRON_NUM_FIELD_RE.match(parts[1])):
        return False
    # dom, month, dow: allow named values (JAN, MON, etc.)
    return all(_CRON_FIELD_RE.match(f) for f in parts[2:5])

_DAY_NAMES = {
    # 3-letter abbreviations (canonical cron form)
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
    # Full names
    "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3,
    "thursday": 4, "friday": 5, "saturday": 6,
}
_DAY_ABBR  = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Ordinal dom keywords; value = n for nth (1-5) or 0 for last.
# Numeric forms (1st/2nd/…/5th) and -1st/-1th are accepted as synonyms.
# 5th uses dom range 29-31 (clamped from 35); fires only on months with a 5th occurrence.
# -1st/-1th mean last weekday of month (same as the word 'last'), NOT last day of month
# (for last day of month use -1 in the dom field).
_ORDINAL_NAMES = {
    "first":  1, "1st": 1,
    "second": 2, "2nd": 2,
    "third":  3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth":  5, "5th": 5,
    "last":   0, "-1st": 0, "-1th": 0,
}

# ── Solar geometry ─────────────────────────────────────────────────────────────

def _solar_event_utc(d: date, lat: float, lon: float, event: str) -> datetime:
    """UTC datetime of a solar event at (lat, lon) on date d."""
    doy = d.timetuple().tm_yday
    B   = 2 * math.pi * (doy - 1) / 365
    decl = (0.006918 - 0.399912*math.cos(B) + 0.070257*math.sin(B)
            - 0.006758*math.cos(2*B) + 0.000907*math.sin(2*B)
            - 0.002697*math.cos(3*B) + 0.001480*math.sin(3*B))
    eot_min = 229.18 * (0.000075 + 0.001868*math.cos(B) - 0.032077*math.sin(B)
                        - 0.014615*math.cos(2*B) - 0.04089*math.sin(2*B))
    noon_utc = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                + timedelta(minutes=720 - 4 * lon - eot_min))

    if event == "solarnoon":
        return noon_utc

    h_deg    = _SOLAR_ELEVATION[event]
    sin_lat  = math.sin(math.radians(lat))
    cos_lat  = math.cos(math.radians(lat))
    sin_decl = math.sin(decl)
    cos_decl = math.cos(decl)
    cos_ha   = (math.sin(math.radians(h_deg)) - sin_lat * sin_decl) / (cos_lat * cos_decl)

    if cos_ha >= 1:
        raise ValueError(f"no {event} on {d} at lat={lat:.2f} (polar night / deep winter)")
    if cos_ha <= -1:
        raise ValueError(f"no {event} on {d} at lat={lat:.2f} (midnight sun / permanent twilight)")

    half_day_min = math.degrees(math.acos(cos_ha)) * 4
    sign = -1 if event in _SOLAR_RISE_EVENTS else 1
    return noon_utc + timedelta(minutes=sign * half_day_min)


# ── Crontab field expansion ────────────────────────────────────────────────────

def _expand_cron_field(expr: str, lo: int, hi: int) -> list[int]:
    """Expand a crontab field expression to a sorted list of integers.
    Supports: N, *, N-M, */S, N-M/S, and comma-separated combinations."""
    result: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if "/" in part:
            base, step_s = part.rsplit("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"step must be >= 1, got {step}")
            if base == "*":
                result.update(range(lo, hi + 1, step))
            elif "-" in base:
                a, b = base.split("-", 1)
                result.update(range(int(a), int(b) + 1, step))
            else:
                result.update(range(int(base), hi + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1))
        elif part == "*":
            result.update(range(lo, hi + 1))
        else:
            result.add(int(part))
    out = sorted(result)
    for v in out:
        if not (lo <= v <= hi):
            raise ValueError(f"value {v} out of range [{lo},{hi}] in {expr!r}")
    return out


# ── System timezone ────────────────────────────────────────────────────────────

def _system_tz() -> ZoneInfo:
    # Resolve /etc/localtime symlink — works on macOS and Linux, no privileges needed.
    try:
        lt = str(Path("/etc/localtime").resolve())
        for marker in ["/zoneinfo/", "\\zoneinfo\\"]:
            if marker in lt:
                return ZoneInfo(lt.split(marker, 1)[1])
    except Exception:
        pass
    # Fall back to the TZ environment variable
    tz_env = os.environ.get("TZ", "")
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except Exception:
            pass
    return ZoneInfo("UTC")


# ── Day-of-week shifting ───────────────────────────────────────────────────────

def _shift_dow(dow: str, delta: int) -> tuple[str, bool]:
    """Shift a crontab dow field by delta days.  Returns (new_dow, ok).
    ok=False means the expression was too complex to shift; caller should warn."""
    if delta == 0 or dow == "*":
        return dow, True

    def shift_token(s: str) -> str:
        s = s.strip()
        low = s.lower()
        if low in _DAY_NAMES:
            return _DAY_ABBR[(_DAY_NAMES[low] + delta) % 7]
        n = int(s)   # raises ValueError if not numeric
        return str((n % 7 + delta) % 7)

    try:
        if "/" in dow:
            return dow, False          # step expression — too complex
        if "," in dow:
            return ",".join(shift_token(p) for p in dow.split(",")), True
        if "-" in dow:
            a, b = dow.split("-", 1)
            return f"{shift_token(a)}-{shift_token(b)}", True
        return shift_token(dow), True
    except (ValueError, IndexError):
        return dow, False


# ── Special dom expansion ──────────────────────────────────────────────────────

def _expand_dom_spec(
    dom: str, month: str, dow: str, command: str
) -> tuple[str, str, str, str, bool, list[str]]:
    """Expand special dom values.

    Returns (dom, month, dow, command, dow_was_set, warnings).
    dow_was_set=True means dom expansion fixed the weekday; dow-modifying
    filters should back off and warn instead of overriding it.
    """

    # Negative offset from end of month: -1 = last day, -2 = 2nd to last, …
    if re.match(r"^-\d+$", dom):
        N = int(dom)                           # e.g. -1
        if N <= -32:
            return (dom, month, dow, command, False, [
                f"# WARNING: dom '{dom}' can never match — no month has {-N} or more days"
            ])
        dom_lo = max(1, 29 + N)               # narrowest safe range
        dom_hi = min(31, 32 + N)
        # Guard: today is the |N|-th day from end of month
        offset = -N - 1                        # 0 = last day, 1 = 2nd to last …
        rhs = "calendar.monthrange(d.year,d.month)[1]" + (f"-{offset}" if offset else "")
        guard = (
            f'python3 -c "import calendar,datetime; d=datetime.date.today(); '
            f'exit(0 if d.day=={rhs} else 1)"'
        )
        return (f"{dom_lo}-{dom_hi}", month, dow,
                f"{guard} || exit 0; {command}", False, [])

    # Ordinal weekday: first/second/third/fourth/last in dom, weekday in dow
    dom_low = dom.lower()
    if dom_low in _ORDINAL_NAMES:
        dow_low = dow.lower()
        if dow_low in _DAY_NAMES:
            cron_dow = _DAY_NAMES[dow_low]
        elif re.match(r"^\d+$", dow):
            cron_dow = int(dow) % 7
        else:
            return (dom, month, dow, command, False, [
                f"# WARNING: ordinal dom '{dom}' requires a weekday name or 0–6 "
                f"in the dow field, got {dow!r}"
            ])

        n = _ORDINAL_NAMES[dom_low]
        if n >= 1:                             # first … fifth: dom range trick
            dom_lo = (n - 1) * 7 + 1
            dom_hi = min(n * 7, 31)           # 5th: 35 → 31; fires only if month has 5th occurrence
            return (f"{dom_lo}-{dom_hi}", month, str(cron_dow),
                    command, True, [])
        else:                                  # last: guard needed
            # python weekday: 0=Mon … 6=Sun; cron dow: 0=Sun, 1=Mon … 6=Sat
            python_wd = (cron_dow - 1) % 7
            guard = (
                f'python3 -c "from datetime import date,timedelta; d=date.today(); '
                f'exit(0 if d.weekday()=={python_wd}'
                f' and (d+timedelta(7)).month!=d.month else 1)"'
            )
            return ("22-31", month, str(cron_dow),
                    f"{guard} || exit 0; {command}", True, [])

    return dom, month, dow, command, False, []


# ── Day filter ─────────────────────────────────────────────────────────────────

def _apply_day_filter(
    days: str, filter_val: str | None, command: str, dow_locked: bool = False
) -> tuple[str, str, list[str]]:
    """Return (new_days, new_command, warning_lines) after applying day filter.

    dow_locked=True means the dom spec already fixed the weekday; filters that
    would override the dow field emit a warning instead.
    """
    if not filter_val:
        return days, command, []

    dom, month, dow = days.split()

    def dow_conflict(filter_name: str) -> tuple[str, str, list[str]]:
        return days, command, [
            f"# WARNING: '{filter_name}' filter conflicts with ordinal-weekday "
            f"dom spec and was ignored"
        ]

    if filter_val == "workday":
        if dow_locked:
            return dow_conflict("workday")
        return f"{dom} {month} 1-5", command, []

    if filter_val == "weekend":
        if dow_locked:
            return dow_conflict("weekend")
        return f"{dom} {month} 0,6", command, []

    if filter_val == "last_dom":
        guard = (
            'python3 -c "import calendar,datetime; d=datetime.date.today(); '
            'exit(0 if d.day==calendar.monthrange(d.year,d.month)[1] else 1)"'
        )
        return f"28-31 {month} {dow}", f"{guard} || exit 0; {command}", []

    if filter_val.startswith("nth_weekday:"):
        if dow_locked:
            return dow_conflict("nth_weekday")
        params = filter_val[len("nth_weekday:"):]
        try:
            n_str, day_str = params.split(",", 1)
            n = int(n_str)
            day_low = day_str.strip().lower()
            cron_dow = _DAY_NAMES[day_low] if day_low in _DAY_NAMES else int(day_str.strip()) % 7
            dom_lo, dom_hi = (n - 1) * 7 + 1, n * 7
        except (ValueError, KeyError) as e:
            return days, command, [f"# WARNING: malformed nth_weekday filter {filter_val!r}: {e}"]
        return f"{dom_lo}-{dom_hi} {month} {cron_dow}", command, []

    if filter_val.startswith("every_n_days:"):
        params = filter_val[len("every_n_days:"):]
        try:
            n_str, ref_str = params.split(",", 1)
            n, ref_str = int(n_str), ref_str.strip()
            yr, mo, dy = ref_str.split("-")
            _ = date(int(yr), int(mo), int(dy))   # validate
        except (ValueError, TypeError) as e:
            return days, command, [
                f"# WARNING: malformed every_n_days filter {filter_val!r}: {e}",
                f"# Expected format: every_n_days:N,YYYY-MM-DD",
            ]
        # Use d-d//n*n instead of d%n — cron treats bare % as a metacharacter
        guard = (
            f'python3 -c "from datetime import date; t=date.today(); '
            f'd=(t-date({int(yr)},{int(mo)},{int(dy)})).days; exit(d-d//{n}*{n})"'
        )
        return days, f"{guard} || exit 0; {command}", []

    if filter_val.startswith("between:"):
        params = filter_val[len("between:"):]
        try:
            start_str, end_str = params.split(",", 1)
            sy, sm, sd = start_str.strip().split("-")
            ey, em, ed = end_str.strip().split("-")
            _ = date(int(sy), int(sm), int(sd))   # validate
            _ = date(int(ey), int(em), int(ed))
        except (ValueError, TypeError) as e:
            return days, command, [
                f"# WARNING: malformed between filter {filter_val!r}: {e}",
                f"# Expected format: between:YYYY-MM-DD,YYYY-MM-DD",
            ]
        guard = (
            f'python3 -c "from datetime import date; t=date.today(); '
            f'exit(0 if date({int(sy)},{int(sm)},{int(sd)})<=t<='
            f'date({int(ey)},{int(em)},{int(ed)}) else 1)"'
        )
        return days, f"{guard} || exit 0; {command}", []

    return days, command, [f"# WARNING: unknown filter {filter_val!r} — ignored"]


# ── Parser ─────────────────────────────────────────────────────────────────────

def _build_job_entry(parts: list[str], lineno: int,
                     current_tz: str | None,
                     current_lat: float | None,
                     current_lon: float | None) -> dict:
    """Build a job entry dict from tokens where parts[0] is the time spec."""
    if len(parts) < 5:
        raise ValueError(f"line {lineno}: tz-aware job needs "
                         f"'time dom month dow command', got {' '.join(parts)!r}")
    if current_tz is None:
        raise ValueError(f"line {lineno}: no '# tz: ...' directive before tz-aware job")
    if current_tz.lower() == "local":
        tz = None   # resolved at compile time from system_tz
    else:
        try:
            tz = ZoneInfo(current_tz)
        except ZoneInfoNotFoundError:
            raise ValueError(f"line {lineno}: unknown timezone {current_tz!r}")

    first   = parts[0]
    time_m  = _TIME_RE.match(first)
    solar_m = _SOLAR_RE.match(first)
    range_m = _SOLAR_RANGE_RE.match(first)

    entry: dict = {
        "type":     "job",
        "timezone": current_tz,
        "tz_obj":   tz,
        "days":     " ".join(parts[1:4]),
        "command":  " ".join(parts[4:]),
    }
    if time_m:
        # Store raw expression (may be multi-time like '9,17:00')
        entry["intended"] = f"{time_m.group(1)}:{time_m.group(2)}"
        if time_m.group(3):
            entry["jitter_min"] = int(time_m.group(3))
    elif solar_m:
        entry["intended"]   = solar_m.group(1)
        entry["offset_min"] = int(solar_m.group(2)) if solar_m.group(2) else 0
        if solar_m.group(3):
            entry["jitter_min"] = int(solar_m.group(3))
        entry["lat"] = current_lat
        entry["lon"] = current_lon
    elif range_m:
        unit = range_m.group(8)
        step_min = int(range_m.group(7)) * (60 if unit == "h" else 1)
        entry["intended"]        = "solar_range"
        entry["range_event1"]    = range_m.group(1)
        entry["range_offset1"]   = int(range_m.group(2)) if range_m.group(2) else 0
        entry["range_jitter1"]   = int(range_m.group(3)) if range_m.group(3) else 0
        entry["range_event2"]    = range_m.group(4)
        entry["range_offset2"]   = int(range_m.group(5)) if range_m.group(5) else 0
        entry["range_jitter2"]   = int(range_m.group(6)) if range_m.group(6) else 0
        entry["range_step_min"]  = step_min
        if range_m.group(9):
            entry["jitter_min"] = int(range_m.group(9))
        entry["lat"] = current_lat
        entry["lon"] = current_lon
    return entry


def parse_source(text: str) -> list[dict]:
    """Parse a crontab_src file into a list of entry dicts."""
    current_tz     = None
    current_lat    = None
    current_lon    = None
    current_filter = None
    current_epoch: str | None = None
    skip_next_job  = False
    entries = []

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line     = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            entries.append({"type": "blank"})
            continue

        if stripped.startswith("#"):
            m = _TZ_RE.search(stripped)
            if m:
                candidate = m.group(1)
                if candidate.lower() in ("none", "off", "clear"):
                    print(f"WARNING line {lineno}: '# tz: {candidate}' is not a valid "
                          f"timezone — to use the system timezone write '# tz: local'; "
                          f"directive ignored", file=sys.stderr)
                else:
                    current_tz = candidate
            m = _LAT_RE.search(stripped)
            if m:
                current_lat = float(m.group(1))
            m = _LON_RE.search(stripped)
            if m:
                current_lon = float(m.group(1))
            m = _FILTER_RE.search(stripped)
            if m:
                val = m.group(1).lower()
                current_filter = None if val in ("none", "off") else val
            elif _FILTER_TYPO_RE.search(stripped):
                print(f"WARNING line {lineno}: looks like a malformed filter directive "
                      f"(missing colon after 'filter'?): {stripped!r}", file=sys.stderr)
            m = _EPOCH_RE.search(stripped)
            if m:
                current_epoch = m.group(1)

            m = _TZ_SRC_RE.match(stripped)
            if m:
                src_parts = m.group(1).split()
                job = _build_job_entry(src_parts, lineno,
                                       current_tz, current_lat, current_lon)
                job["filter"] = current_filter
                entries.append({"type": "comment", "text": line})
                entries.append(job)
                skip_next_job = True
            else:
                entries.append({"type": "comment", "text": line})
            continue

        if re.match(r"^\w+=", stripped) and " " not in stripped.split("=")[0]:
            entries.append({"type": "raw", "line": line})
            continue

        if skip_next_job:
            skip_next_job = False
            continue

        parts      = stripped.split()
        first      = parts[0] if parts else ""
        time_m     = _TIME_RE.match(first)
        solar_m    = _SOLAR_RE.match(first)
        range_m    = _SOLAR_RANGE_RE.match(first)
        interval_m = _TIME_INTERVAL_RE.match(first)

        if time_m or solar_m or range_m:
            job = _build_job_entry(parts, lineno, current_tz, current_lat, current_lon)
            job["filter"] = current_filter
            entries.append({"type": "tz_src_comment", "tokens": parts})
            entries.append(job)
        elif interval_m:
            ok = True
            if len(parts) < 5:
                print(f"WARNING line {lineno}: interval job needs "
                      f"'HH:MM/Nm dom month dow command', got {stripped!r}", file=sys.stderr)
                ok = False
            elif current_tz is None:
                print(f"WARNING line {lineno}: no '# tz:' directive before interval job",
                      file=sys.stderr)
                ok = False
            if ok:
                try:
                    tz_obj = (None if current_tz.lower() == "local"  # type: ignore[union-attr]
                              else ZoneInfo(current_tz))              # type: ignore[arg-type]
                except ZoneInfoNotFoundError:
                    print(f"WARNING line {lineno}: unknown timezone {current_tz!r}",
                          file=sys.stderr)
                    ok = False
            if ok:
                inline_date = interval_m.group(1)       # YYYY-MM-DD or None
                anchor_hh  = int(interval_m.group(2))
                anchor_mm  = int(interval_m.group(3))
                interval_n = int(interval_m.group(4))
                unit       = interval_m.group(5)        # 'm' or 'd'
                jitter     = int(interval_m.group(6)) if interval_m.group(6) else 0
                eff_tz     = tz_obj or _system_tz()
                if inline_date:
                    try:
                        date.fromisoformat(inline_date)
                    except ValueError:
                        print(f"WARNING line {lineno}: invalid date {inline_date!r} "
                              f"in interval spec {first!r} — line skipped", file=sys.stderr)
                        ok = False

                if unit == 'm':
                    # Epoch precedence: inline date > # epoch: > HH:MM today
                    if inline_date:
                        epoch_str = f"{inline_date}T{anchor_hh:02d}:{anchor_mm:02d}"
                    elif current_epoch:
                        epoch_str = current_epoch
                    else:
                        today_in_tz = datetime.now(tz=eff_tz).date()
                        epoch_str = (f"{today_in_tz.isoformat()}"
                                     f"T{anchor_hh:02d}:{anchor_mm:02d}")
                    entries.append({
                        "type":         "every_interval",
                        "timezone":     current_tz,
                        "tz_obj":       tz_obj,
                        "interval_min": interval_n,
                        "jitter_min":   jitter,
                        "epoch":        epoch_str,
                        "days":         " ".join(parts[1:4]),
                        "command":      " ".join(parts[4:]),
                        "filter":       current_filter,
                        "src_tokens":   parts,
                    })

                else:  # unit == 'd': single daily firing with every_n_days filter
                    if inline_date:
                        ref_date = inline_date
                    elif current_epoch:
                        ref_date = current_epoch.split("T")[0]
                    else:
                        ref_date = datetime.now(tz=eff_tz).date().isoformat()
                    anchor_spec = f"{anchor_hh:02d}:{anchor_mm:02d}"
                    if jitter:
                        anchor_spec += f"~{jitter}"
                    job = _build_job_entry(
                        [anchor_spec] + parts[1:], lineno,
                        current_tz, current_lat, current_lon
                    )
                    job["filter"] = f"every_n_days:{interval_n},{ref_date}"
                    entries.append({"type": "tz_src_comment", "tokens": parts})
                    entries.append(job)
            else:
                entries.append({"type": "raw", "line": line})
        else:
            if any(first.startswith(ev) for ev in _ALL_SOLAR_EVENTS):
                print(f"WARNING line {lineno}: first field looks like a solar event "
                      f"but is not a recognized expression — "
                      f"solar events accept only [+-]<minutes> offsets and ~<jitter> "
                      f"(e.g. 'sunrise+30', 'civil_dawn~5', 'solarnoon'): "
                      f"{stripped!r}", file=sys.stderr)
            elif not _is_valid_crontab_line(parts):
                print(f"WARNING line {lineno}: unrecognized syntax "
                      f"(not a .crontab_src time/solar spec, not a valid crontab line): "
                      f"{stripped!r}", file=sys.stderr)
            entries.append({"type": "raw", "line": line})

    return entries


# ── Compiler ───────────────────────────────────────────────────────────────────

def _compile_job(entry: dict, system_tz: ZoneInfo) -> list[str]:
    """Return a list of lines: optional warning/info comments + cron line(s)."""
    today      = datetime.now(tz=system_tz).date()
    tz         = entry["tz_obj"] or system_tz   # None means # tz: local
    intended   = entry["intended"]
    jitter     = entry.get("jitter_min", 0)
    days_base  = entry["days"]
    filter_val = entry.get("filter")

    # Build list of local datetimes for each expanded time
    if intended in _ALL_SOLAR_EVENTS:
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is None or lon is None:
            raise ValueError(f"solar job needs lat/lon: {entry['command']!r}")
        dt_utc  = _solar_event_utc(today, lat, lon, intended)
        dt_utc += timedelta(minutes=entry.get("offset_min", 0))
        if jitter:
            dt_utc += timedelta(minutes=random.randint(0, jitter))
        local_times = [dt_utc.astimezone(system_tz)]
    elif intended == "solar_range":
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is None or lon is None:
            raise ValueError(f"solar range needs lat/lon: {entry['command']!r}")
        rj1 = entry.get("range_jitter1", 0)
        rj2 = entry.get("range_jitter2", 0)
        rj1_td = timedelta(minutes=random.randint(0, rj1)) if rj1 else timedelta(0)
        rj2_td = timedelta(minutes=random.randint(0, rj2)) if rj2 else timedelta(0)
        dt1 = (_solar_event_utc(today, lat, lon, entry["range_event1"])
               + timedelta(minutes=entry.get("range_offset1", 0))
               + rj1_td)
        dt2 = (_solar_event_utc(today, lat, lon, entry["range_event2"])
               + timedelta(minutes=entry.get("range_offset2", 0))
               + rj2_td)
        if dt2 <= dt1:
            # event2 precedes event1 today — try tomorrow's event2.
            # Handles overnight ranges like sunset-sunrise where sunrise is next morning.
            # Reuse the same rj2_td so the endpoint jitter is consistent.
            dt2 = (_solar_event_utc(today + timedelta(days=1), lat, lon,
                                    entry["range_event2"])
                   + timedelta(minutes=entry.get("range_offset2", 0))
                   + rj2_td)
        if dt2 <= dt1:
            raise ValueError(
                f"solar range: {entry['range_event2']} is not after "
                f"{entry['range_event1']} on {today} "
                f"(got {dt1.strftime('%H:%M')}–{dt2.strftime('%H:%M')} UTC)")
        step = timedelta(minutes=entry["range_step_min"])
        local_times = []
        t = dt1
        while t <= dt2:
            jitter_td = timedelta(minutes=random.randint(0, jitter)) if jitter else timedelta(0)
            local_times.append((t + jitter_td).astimezone(system_tz))
            t += step
    else:
        hh_expr, mm_expr = intended.split(":", 1)
        try:
            hh_vals = _expand_cron_field(hh_expr, 0, 23)
            mm_vals = _expand_cron_field(mm_expr, 0, 59)
        except ValueError as e:
            raise ValueError(f"invalid time spec {intended!r}: {e}") from e
        local_times = []
        for hh in hh_vals:
            for mm in mm_vals:
                dt_local = datetime(today.year, today.month, today.day, hh, mm,
                                    tzinfo=tz).astimezone(system_tz)
                if jitter:
                    dt_local += timedelta(minutes=random.randint(0, jitter))
                local_times.append(dt_local)

    # Collapse solar-range output to fewer cron lines where possible.
    if intended == "solar_range" and not jitter and len(local_times) >= 2:
        step_min = entry["range_step_min"]

        if step_min % 60 == 0:
            # Whole-hour step: every firing shares the same minute; each calendar
            # day's hours form an arithmetic sequence → one range line per day.
            # E.g., /1h across midnight → "41 20-23 * * * cmd" + "41 0-10 * * * cmd".
            step_h = step_min // 60
            if len({t.minute for t in local_times}) == 1:
                mm = local_times[0].minute
                day_groups: dict[int, list[int]] = {}
                for t in local_times:
                    delta = (t.date() - today).days
                    day_groups.setdefault(delta, []).append(t.hour)
                ok = True
                # (hr_expr, cdays, command, prefix_comments)
                group_results: list[tuple[str, str, str, list[str]]] = []
                for delta in sorted(day_groups):
                    hours = day_groups[delta]
                    if hours != list(range(hours[0],
                                          hours[0] + len(hours) * step_h, step_h)):
                        ok = False
                        break
                    h1, h_last = hours[0], hours[-1]
                    hr_expr = (f"{h1}-{h_last}" if step_h == 1
                               else f"{h1}-{h_last}/{step_h}")
                    days = days_base
                    extra: list[str] = []
                    if delta != 0:
                        dom, month, dow = days.split()
                        new_dow, shift_ok = _shift_dow(dow, delta)
                        if shift_ok and new_dow != dow:
                            extra.append(
                                f"# dow shifted {delta:+d} (solar_range crosses midnight)"
                            )
                            days = f"{dom} {month} {new_dow}"
                        elif not shift_ok:
                            extra.append(
                                f"# WARNING: solar_range crosses midnight; "
                                f"dow field '{days.split()[2]}' may need manual adjustment"
                            )
                    dom, month, dow = days.split()
                    dom, month, dow, command, dow_locked, dom_warnings = _expand_dom_spec(
                        dom, month, dow, entry["command"]
                    )
                    cdays = f"{dom} {month} {dow}"
                    cdays, command, filter_warnings = _apply_day_filter(
                        cdays, filter_val, command, dow_locked=dow_locked
                    )
                    group_results.append(
                        (hr_expr, cdays, command,
                         extra + dom_warnings + filter_warnings)
                    )
                if ok:
                    # If all groups share the same days+command (no effective dow
                    # shift), merge into one line: "41 20-23,0-10 * * * cmd"
                    if (len(group_results) > 1
                            and len({(c, cmd)
                                     for _, c, cmd, _ in group_results}) == 1):
                        _, cdays, command, _ = group_results[0]
                        all_comments = [ln for _, _, _, pfx in group_results
                                        for ln in pfx]
                        hr_list = ",".join(hr for hr, _, _, _ in group_results)
                        return all_comments + [f"{mm} {hr_list} {cdays} {command}"]
                    collapsed: list[str] = []
                    for hr_expr, cdays, command, pfx in group_results:
                        collapsed.extend(pfx + [f"{mm} {hr_expr} {cdays} {command}"])
                    return collapsed

        elif step_min < 60 and 60 % step_min == 0 \
                and all(t.date() == today for t in local_times):
            # Sub-hour step dividing 60: interior hours share the same minute pattern.
            # E.g., /30m with sunrise at 5:47 → "17,47 6-19 * * * cmd" plus
            # partial-hour lines for the boundary hours (5:47 and 20:17).
            m0 = local_times[0].minute
            cycle = sorted({(m0 + k * step_min) % 60 for k in range(60 // step_min)})
            full_set = frozenset(cycle)
            hour_minutes: dict[int, list[int]] = {}
            for t in local_times:
                hour_minutes.setdefault(t.hour, []).append(t.minute)
            hours_sorted = sorted(hour_minutes)
            interior = [h for h in hours_sorted if frozenset(hour_minutes[h]) == full_set]
            if interior and interior == list(range(interior[0], interior[-1] + 1)):
                dom, month, dow = days_base.split()
                dom, month, dow, command, dow_locked, dom_warnings = _expand_dom_spec(
                    dom, month, dow, entry["command"]
                )
                cdays = f"{dom} {month} {dow}"
                cdays, command, filter_warnings = _apply_day_filter(
                    cdays, filter_val, command, dow_locked=dow_locked
                )
                def _rline(mins: list[int], h_expr: str) -> str:
                    return (f"{','.join(str(m) for m in sorted(mins))}"
                            f" {h_expr} {cdays} {command}")
                result = dom_warnings + filter_warnings
                for h in [h for h in hours_sorted if h < interior[0]]:
                    result.append(_rline(hour_minutes[h], str(h)))
                h1, h_last = interior[0], interior[-1]
                result.append(_rline(cycle, f"{h1}-{h_last}" if h1 != h_last else str(h1)))
                for h in [h for h in hours_sorted if h > interior[-1]]:
                    result.append(_rline(hour_minutes[h], str(h)))
                return result

    result_lines: list[str] = []
    for dt_local in local_times:
        day_delta = (dt_local.date() - today).days
        days = days_base
        extra: list[str] = []

        # Midnight-crossing dow adjustment
        if day_delta != 0:
            dom, month, dow = days.split()
            new_dow, ok = _shift_dow(dow, day_delta)
            if ok and new_dow != dow:
                extra.append(
                    f"# dow shifted {day_delta:+d} ({intended} {entry['timezone']} "
                    f"→ {dt_local.strftime('%H:%M')} {system_tz.key}, crosses midnight)"
                )
                days = f"{dom} {month} {new_dow}"
            elif not ok:
                extra.append(
                    f"# WARNING: {intended} {entry['timezone']} crosses midnight into "
                    f"{system_tz.key}; dow field '{days.split()[2]}' may need manual adjustment"
                )

        # Special dom expansion (ordinal weekdays, negative offsets)
        dom, month, dow = days.split()
        dom, month, dow, command, dow_locked, dom_warnings = _expand_dom_spec(
            dom, month, dow, entry["command"]
        )
        days = f"{dom} {month} {dow}"
        extra.extend(dom_warnings)

        # Day filter
        days, command, filter_warnings = _apply_day_filter(
            days, filter_val, command, dow_locked=dow_locked
        )
        extra.extend(filter_warnings)

        result_lines.extend(extra + [f"{dt_local.minute} {dt_local.hour} {days} {command}"])

    return result_lines


def _compile_every(entry: dict, system_tz: ZoneInfo) -> list[str]:
    """Enumerate HH:MM/Nm firings within today in system_tz and emit cron lines."""
    today_sys = datetime.now(tz=system_tz).date()
    tz = entry["tz_obj"] or system_tz

    epoch_naive = datetime.strptime(entry["epoch"], "%Y-%m-%dT%H:%M")
    epoch_dt    = epoch_naive.replace(tzinfo=tz)

    interval_min = entry["interval_min"]
    jitter       = entry.get("jitter_min", 0)
    filter_val   = entry.get("filter")

    today_start = datetime(today_sys.year, today_sys.month, today_sys.day,
                           tzinfo=system_tz)
    today_end = today_start + timedelta(days=1)

    # First k s.t. epoch + k*interval >= today_start
    delta_s = (today_start - epoch_dt.astimezone(system_tz)).total_seconds()
    k0 = max(0, math.ceil(delta_s / 60 / interval_min))

    src_tokens = entry.get("src_tokens")
    header = ("# [every] " + " ".join(src_tokens)) if src_tokens else f"# [every:{interval_min}m]"

    # Dom expansion and filter application are the same for every firing — do once.
    dom, month, dow = entry["days"].split()
    dom, month, dow, command, dow_locked, dom_warnings = _expand_dom_spec(
        dom, month, dow, entry["command"]
    )
    cdays = f"{dom} {month} {dow}"
    cdays, command, filter_warnings = _apply_day_filter(
        cdays, filter_val, command, dow_locked=dow_locked
    )
    preamble = dom_warnings + filter_warnings

    firing_lines: list[str] = []
    k = k0
    while True:
        t_sys = (epoch_dt + timedelta(minutes=k * interval_min)).astimezone(system_tz)
        if t_sys >= today_end:
            break
        if t_sys >= today_start:
            if jitter:
                t_sys = t_sys + timedelta(minutes=random.randint(0, jitter))
            firing_lines.append(f"{t_sys.minute} {t_sys.hour} {cdays} {command}")
        k += 1

    if not firing_lines:
        return [header] + preamble + [f"# no firings today for {interval_min}m interval"]

    return [header] + preamble + firing_lines


def compile_crontab(entries: list[dict], system_tz: ZoneInfo,
                    src_path: Path) -> list[str]:
    now_str = datetime.now(tz=system_tz).strftime("%Y-%m-%d %H:%M")
    lines = [f"# Generated {now_str}  system: {system_tz.key}  src: {src_path}"]
    for entry in entries:
        t = entry["type"]
        if t == "blank":
            lines.append("")
        elif t == "comment":
            lines.append(entry["text"])
        elif t == "tz_src_comment":
            lines.append("# [tz-src] " + " ".join(entry["tokens"]))
        elif t == "raw":
            lines.append(entry["line"])
        elif t == "job":
            lines.extend(_compile_job(entry, system_tz))
        elif t == "every_interval":
            lines.extend(_compile_every(entry, system_tz))
    return lines


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=META,
                    help="Source file (default: ~/.crontab_src)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print generated crontab without installing it")
    args = ap.parse_args()

    if not args.src.exists():
        print(f"Source file not found: {args.src}", file=sys.stderr)
        print("Create it — see the docstring in this script for an example.", file=sys.stderr)
        sys.exit(1)

    lock_path = Path(f"/tmp/adjust_cron_tz_{Path.home().name}.lock")
    lock_fh   = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance is running; exiting.", file=sys.stderr)
        sys.exit(0)

    try:
        system_tz = _system_tz()
        src_text  = args.src.read_text()
        entries   = parse_source(src_text)
        lines     = compile_crontab(entries, system_tz, args.src.resolve())
        crontab   = "\n".join(lines) + "\n"

        print(f"System timezone: {system_tz.key}")
        print("Generated crontab:")
        print("─" * 60)
        print(crontab, end="")
        print("─" * 60)

        if args.dry_run:
            print("(dry run — not installed)")
            return

        subprocess.run(["crontab", "-"], input=crontab.encode(), check=True)
        print("Installed.")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    main()
