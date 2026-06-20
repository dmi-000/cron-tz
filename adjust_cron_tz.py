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
     # lat: 51.50  lon: -0.12

   Apply to subsequent tz-aware jobs until overridden by another directive.

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
     none                                 clear filter (runs every day)

   workday/weekend modify the crontab dow field directly.  The others wrap
   the command in a python3 one-liner guard (requires python3 in PATH at
   job runtime).  Filtered jobs exit 0 silently on non-matching days.

   The reference date in every_n_days is the phase: change it to shift
   which days the cycle lands on.

Jitter
------
Appending ~N to any time spec adds a random delay of 0–N minutes at
compile time.  Useful for avoiding thundering-herd when multiple machines
run the same job.  A new random offset is chosen on each recompile.

     08:00~10  * * * python3 ~/bin/job.py   # fires 08:00–08:10

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
  06:00~5        * * *  python3 ~/bin/morning_job.py

  # filter: none
  civil_dusk+15  * * *  python3 ~/bin/lights_on.py
  sunset+30      * * *  python3 ~/bin/evening_job.py

  # UTC — raw lines pass through unchanged
  0 4 * * 0 /usr/bin/weekly-backup.sh
"""

import argparse
import fcntl
import math
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

# Directive patterns (searched anywhere in a comment line)
_TZ_RE     = re.compile(r"\btz\s*:\s*(\S+)")
_LAT_RE    = re.compile(r"\blat\s*:\s*(-?\d+(?:\.\d*)?)")
_LON_RE    = re.compile(r"\blon\s*:\s*(-?\d+(?:\.\d*)?)")
_FILTER_RE = re.compile(r"\bfilter\s*:\s*(\S+)")
_TZ_SRC_RE = re.compile(r"^#\s*\[tz-src\]\s+(.+)$")

# Heuristic to catch '# filter keyword' with a missing colon
_FILTER_TYPO_RE = re.compile(
    r"\bfilter\s+(?:workday|weekend|last_dom|nth_weekday|every_n_days|between|none|off)\b"
)

_DAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_DAY_ABBR  = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


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
    # macOS
    try:
        out = subprocess.check_output(
            ["systemsetup", "-gettimezone"], stderr=subprocess.DEVNULL
        ).decode()
        name = out.split("Time Zone:")[-1].strip()
        if name:
            return ZoneInfo(name)
    except Exception:
        pass
    # Linux / other: resolve /etc/localtime symlink
    try:
        lt = str(Path("/etc/localtime").resolve())
        for marker in ["/zoneinfo/", "\\zoneinfo\\"]:
            if marker in lt:
                return ZoneInfo(lt.split(marker, 1)[1])
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
        if re.search(r"[A-Za-z]", dow) and "-" in dow:
            a, b = dow.split("-", 1)
            return f"{shift_token(a)}-{shift_token(b)}", True
        if "-" in dow:
            a, b = dow.split("-", 1)
            return f"{shift_token(a)}-{shift_token(b)}", True
        return shift_token(dow), True
    except (ValueError, IndexError):
        return dow, False


# ── Day filter ─────────────────────────────────────────────────────────────────

def _apply_day_filter(
    days: str, filter_val: str | None, command: str
) -> tuple[str, str, list[str]]:
    """Return (new_days, new_command, warning_lines) after applying day filter."""
    if not filter_val:
        return days, command, []

    dom, month, dow = days.split()

    if filter_val == "workday":
        return f"{dom} {month} 1-5", command, []

    if filter_val == "weekend":
        return f"{dom} {month} 0,6", command, []

    if filter_val == "last_dom":
        # Narrow dom to 28-31 (only candidates for last day) + python3 guard
        guard = (
            'python3 -c "import calendar,datetime; d=datetime.date.today(); '
            'exit(0 if d.day==calendar.monthrange(d.year,d.month)[1] else 1)"'
        )
        return f"28-31 {month} {dow}", f"{guard} || exit 0; {command}", []

    if filter_val.startswith("nth_weekday:"):
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
        guard = (
            f'python3 -c "from datetime import date; t=date.today(); '
            f'exit(0 if (t-date({int(yr)},{int(mo)},{int(dy)})).days%{n}==0 else 1)"'
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
    try:
        tz = ZoneInfo(current_tz)
    except ZoneInfoNotFoundError:
        raise ValueError(f"line {lineno}: unknown timezone {current_tz!r}")

    first   = parts[0]
    time_m  = _TIME_RE.match(first)
    solar_m = _SOLAR_RE.match(first)

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
    return entry


def parse_source(text: str) -> list[dict]:
    """Parse a crontab_src file into a list of entry dicts."""
    current_tz     = None
    current_lat    = None
    current_lon    = None
    current_filter = None
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
                current_tz = m.group(1)
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

        parts   = stripped.split()
        first   = parts[0] if parts else ""
        time_m  = _TIME_RE.match(first)
        solar_m = _SOLAR_RE.match(first)

        if time_m or solar_m:
            job = _build_job_entry(parts, lineno, current_tz, current_lat, current_lon)
            job["filter"] = current_filter
            entries.append({"type": "tz_src_comment", "tokens": parts})
            entries.append(job)
        else:
            entries.append({"type": "raw", "line": line})

    return entries


# ── Compiler ───────────────────────────────────────────────────────────────────

def _compile_job(entry: dict, system_tz: ZoneInfo) -> list[str]:
    """Return a list of lines: optional warning/info comments + cron line(s)."""
    today      = datetime.now(tz=system_tz).date()
    tz         = entry["tz_obj"]
    intended   = entry["intended"]
    jitter     = entry.get("jitter_min", 0)
    days_base  = entry["days"]
    filter_val = entry.get("filter")

    # Build list of (dt_local,) for each expanded time
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

    result_lines: list[str] = []
    for dt_local in local_times:
        day_delta = (dt_local.date() - today).days
        days = days_base
        extra: list[str] = []

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

        days, command, filter_warn = _apply_day_filter(days, filter_val, entry["command"])
        extra.extend(filter_warn)
        result_lines.extend(extra + [f"{dt_local.minute} {dt_local.hour} {days} {command}"])

    return result_lines


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
