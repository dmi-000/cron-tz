#!/usr/bin/env python3
"""
adjust_cron_tz.py — Crontab compiler with timezone and solar time support.

Source of truth: ~/.crontab_src  (edit this, not crontab directly)
Output: overwrites crontab via `crontab -`

Run on wake, on network change, or daily to keep cron times current when
the system timezone changes or to update sunset/sunrise times seasonally.

Source file format
------------------
Mostly looks like a crontab.  The first field of each line determines how
it is treated — no other context matters:

  HH:MM           → tz-aware fixed-time job (requires active # tz: directive)
  HH:MM~N         → same, with up to N minutes of random jitter
  sunrise[±M][~N] → tz-aware solar job      (requires # tz: and # lat/lon)
  sunset[±M][~N]  → tz-aware solar job      (requires # tz: and # lat/lon)
  anything else   → passed through to crontab unchanged (digit, *, @, KEY=, #)

Standard 5-field cron lines are always raw, even inside a # tz: section.
No closing directive is needed; # tz: only affects lines whose first field
is HH:MM, sunrise, or sunset.

Three extensions over plain crontab:

1. Timezone/location directives in comments:

     # tz: Europe/London
     # lat: 51.50  lon: -0.12

   Apply to subsequent tz-aware jobs until overridden by another directive.

2. Fixed-time jobs in the directive timezone (HH:MM replaces min+hour):

     06:00 * * * python3 ~/bin/morning_job.py

3. Solar-time jobs (±M = minute offset, ~N = jitter up to N minutes):

     sunset+30    * * * python3 ~/bin/evening_job.py
     sunrise-15~5 * * * python3 ~/bin/dawn_job.py

   Compiled to today's solar event time; re-run this script daily to keep
   them current (sunset/sunrise drift ~1 min/day near solstices).

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
     # [tz-src] 06:00 * * * python3 ~/bin/morning_job.py
     0 6 * * * python3 ~/bin/morning_job.py

When the generated crontab is used as src, [tz-src] lines are parsed as
the job source and the compiled line immediately following is skipped and
replaced.  Raw lines added directly to the crontab have no preceding
[tz-src] comment and pass through untouched.

Example ~/.crontab_src
----------------------
  SHELL=/bin/bash
  PATH=/usr/local/bin:/usr/bin:/bin

  # recompile daily at 03:00 UTC to update sunset time
  0 3 * * * python3 ~/bin/adjust_cron_tz.py

  # tz: Europe/London
  # lat: 51.50  lon: -0.12
  06:00~5   * * *   python3 ~/bin/morning_job.py
  sunset+30 * * *   python3 ~/bin/evening_job.py

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

# First-field patterns for tz-aware jobs; group 3 captures optional ~N jitter
_TIME_RE  = re.compile(r"^(\d{1,2}):(\d{2})(?:~(\d+))?$")
_SOLAR_RE = re.compile(r"^(sunrise|sunset)([+-]\d+)?(?:~(\d+))?$")

# Directive patterns (searched anywhere in a comment line)
_TZ_RE     = re.compile(r"\btz\s*:\s*(\S+)")
_LAT_RE    = re.compile(r"\blat\s*:\s*(-?\d+(?:\.\d*)?)")
_LON_RE    = re.compile(r"\blon\s*:\s*(-?\d+(?:\.\d*)?)")
_TZ_SRC_RE = re.compile(r"^#\s*\[tz-src\]\s+(.+)$")

_DAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_DAY_ABBR  = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


# ── Solar geometry ─────────────────────────────────────────────────────────────

def _solar_event_utc(d: date, lat: float, lon: float, event: str) -> datetime:
    """UTC datetime of sunrise or sunset at (lat, lon) on date d."""
    doy = d.timetuple().tm_yday
    B   = 2 * math.pi * (doy - 1) / 365
    decl = (0.006918 - 0.399912*math.cos(B) + 0.070257*math.sin(B)
            - 0.006758*math.cos(2*B) + 0.000907*math.sin(2*B)
            - 0.002697*math.cos(3*B) + 0.001480*math.sin(3*B))
    cos_ha = -math.tan(math.radians(lat)) * math.tan(decl)
    if cos_ha >= 1:
        raise ValueError(f"polar night on {d} at lat={lat:.2f}")
    if cos_ha <= -1:
        raise ValueError(f"midnight sun on {d} at lat={lat:.2f}")
    half_day_min = math.degrees(math.acos(cos_ha)) * 4
    eot_min = 229.18 * (0.000075 + 0.001868*math.cos(B) - 0.032077*math.sin(B)
                        - 0.014615*math.cos(2*B) - 0.04089*math.sin(2*B))
    noon_utc = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                + timedelta(minutes=720 - 4 * lon - eot_min))
    return noon_utc + timedelta(minutes=half_day_min if event == "sunset" else -half_day_min)


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
            # named range like Mon-Fri
            a, b = dow.split("-", 1)
            return f"{shift_token(a)}-{shift_token(b)}", True
        if "-" in dow:
            a, b = dow.split("-", 1)
            return f"{shift_token(a)}-{shift_token(b)}", True
        return shift_token(dow), True
    except (ValueError, IndexError):
        return dow, False


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
        "type":    "job",
        "timezone": current_tz,
        "tz_obj":  tz,
        "days":    " ".join(parts[1:4]),
        "command": " ".join(parts[4:]),
    }
    if time_m:
        entry["intended"] = f"{int(time_m.group(1)):02d}:{time_m.group(2)}"
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
    current_tz    = None
    current_lat   = None
    current_lon   = None
    skip_next_job = False
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

            m = _TZ_SRC_RE.match(stripped)
            if m:
                src_parts = m.group(1).split()
                job = _build_job_entry(src_parts, lineno,
                                       current_tz, current_lat, current_lon)
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
            entries.append({"type": "tz_src_comment", "tokens": parts})
            entries.append(job)
        else:
            entries.append({"type": "raw", "line": line})

    return entries


# ── Compiler ───────────────────────────────────────────────────────────────────

def _compile_job(entry: dict, system_tz: ZoneInfo) -> list[str]:
    """Return a list of lines: optional warning/info comments + the cron line."""
    today    = datetime.now(tz=system_tz).date()
    tz       = entry["tz_obj"]
    intended = entry["intended"]
    jitter   = entry.get("jitter_min", 0)

    if intended in ("sunrise", "sunset"):
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is None or lon is None:
            raise ValueError(f"solar job needs lat/lon: {entry['command']!r}")
        dt_utc  = _solar_event_utc(today, lat, lon, intended)
        dt_utc += timedelta(minutes=entry.get("offset_min", 0))
        if jitter:
            dt_utc += timedelta(minutes=random.randint(0, jitter))
        dt_local = dt_utc.astimezone(system_tz)
    else:
        hh, mm   = map(int, intended.split(":"))
        dt_local = datetime(today.year, today.month, today.day, hh, mm,
                            tzinfo=tz).astimezone(system_tz)
        if jitter:
            dt_local = dt_local + timedelta(minutes=random.randint(0, jitter))

    # Adjust dow if the time conversion crossed midnight
    day_delta = (dt_local.date() - today).days
    days = entry["days"]
    extra_comments: list[str] = []

    if day_delta != 0:
        dom, month, dow = days.split()
        new_dow, ok = _shift_dow(dow, day_delta)
        if ok and new_dow != dow:
            extra_comments.append(
                f"# dow shifted {day_delta:+d} ({intended} {entry['timezone']} "
                f"→ {dt_local.strftime('%H:%M')} {system_tz.key}, crosses midnight)"
            )
            days = f"{dom} {month} {new_dow}"
        elif not ok:
            extra_comments.append(
                f"# WARNING: {intended} {entry['timezone']} crosses midnight into "
                f"{system_tz.key}; dow field '{dow}' may need manual adjustment"
            )

    return extra_comments + [f"{dt_local.minute} {dt_local.hour} {days} {entry['command']}"]


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
        # Read source while holding the lock so a concurrent edit doesn't tear the read.
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
