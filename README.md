# crontabctl

A crontab compiler and daemon for macOS that adds timezone awareness, solar events,
and irrational-interval sampling to standard cron.

`crontabctl` maintains a richer source file (`~/.crontab.in`) and compiles it to a
standard crontab. Run as a daemon it watches the source file with kqueue and
recompiles immediately on changes, keeping cron's schedule current without polling.

## What it adds over plain crontab

| Feature | Example |
|---|---|
| Timezone-aware fixed times | `08:00~10  * * *  cmd` (fires at 08:00 in the named TZ even when the system TZ differs) |
| Solar events | `sunrise * * *  cmd`, `sunset+30 * * *  cmd` |
| Solar ranges | `sunrise-30-sunset+75/1h  * * *  cmd` |
| Irrational-interval sampling | `2026-01-01T00:00/551m  * * *  cmd` |
| Day filters | `# filter: workday`, `every_n_days:5,2026-06-17`, `nth_weekday:2,Mon` |
| Jitter | `08:00~15  * * *  cmd` (fires 08:00–08:15, new offset each recompile) |
| File-watch daemon | recompiles immediately when source is edited |

## Installation

```bash
cp cronsrc.py ~/.local/bin/crontabctl
chmod +x ~/.local/bin/crontabctl
```

Create `~/.crontab.in` (see format below), then:

```bash
crontabctl --dry-run   # preview generated crontab
crontabctl             # compile and install
```

## Running as a daemon

Add to `~/.zlogin` (or any login-time shell file):

```zsh
_ct_pid="$HOME/.local/share/crontabctl/loop.pid"
if ! { [[ -f "$_ct_pid" ]] && kill -0 "$(<$_ct_pid)" 2>/dev/null; }; then
    crontabctl --daemon >> "$HOME/log/crontabctl.log" 2>&1
fi
unset _ct_pid
```

`--daemon` forks, creates a new session, and writes its PID to
`~/.local/share/crontabctl/loop.pid`. It then loops:

1. Compile and install the crontab.
2. Watch `~/.crontab.in` with kqueue — recompile immediately on any write.
3. After 24 hours, recompile to refresh solar times and irrational-interval phases.

`--loop` runs the same cycle without forking (useful when backgrounding externally).

**macOS note:** Do not use a LaunchAgent (Aqua session context triggers a TCC
permission dialog for `/usr/bin/crontab`). A terminal login session works correctly.

## Source file format

Lines whose first field is a standard cron field (`*`, a number, `@reboot`, etc.)
pass through to the generated crontab unchanged. Lines whose first field is
`HH:MM`, a solar event name, or an interval spec are compiled.

### Timezone directive

```
# tz: America/Los_Angeles
# tz: local          # follow the system timezone
```

Applies to all compiled lines that follow until overridden. Required for fixed-time
and solar jobs.

### Location directive

```
# lat: 33.52  lon: -117.71
```

Required for solar events.

### Fixed-time jobs

```
# tz: America/Los_Angeles
08:00        * * *  python3 ~/bin/morning.py
08:00~15     * * *  python3 ~/bin/morning.py   # jitter 0–15 min
9,17:00      * * *  python3 ~/bin/twice.py     # 09:00 and 17:00
9-17/2:00    * * *  python3 ~/bin/every2h.py  # every 2h, 09:00–17:00
```

When the system timezone differs from `# tz:`, times are converted automatically.
If a conversion crosses midnight the dow field is shifted ±1 with a comment.

### Solar events

```
# tz: America/Los_Angeles
# lat: 33.52  lon: -117.71
sunrise            * * *  python3 ~/bin/at_dawn.py
sunset+30          * * *  python3 ~/bin/thirty_after_dusk.py
civil_dawn-15~5    * * *  python3 ~/bin/early.py
```

Available events: `sunrise`, `sunset`, `civil_dawn`, `civil_dusk`,
`nautical_dawn`, `nautical_dusk`, `astronomical_dawn`, `astronomical_dusk`,
`solarnoon`.

All accept an optional `±M` minute offset and `~N` jitter suffix.

### Solar ranges

```
sunrise-30-sunset+75/1h   * * *  python3 ~/bin/hourly_daytime.py
sunrise~30-sunset/2h~10   * * *  python3 ~/bin/job.py
```

Syntax: `event1[±off][~J] - event2[±off][~J] /N(m|h) [~J]`

Jitter on an endpoint shifts the boundary once (all firings march uniformly).
Jitter at the end shifts each firing independently.

Whole-hour steps collapse to an hour-range line when possible:

```
# generated output for /1h across the day:
32 20-23,0-11 * * *  python3 ~/bin/hourly_daytime.py
```

### Interval firing

```
2026-06-24T02:00/551m  * * *  python3 ~/bin/fetch.py
2026-06-24T02:00/551m~5  * * *  python3 ~/bin/fetch.py   # ±5 min jitter per firing
```

At each compile the script walks forward from the epoch in N-minute steps and emits
one cron line per firing that falls in the next 24 hours. With a fixed epoch, firing
times drift by `N mod 1440` minutes per day across recompiles.

551 minutes (≈ 1440 / φ²) is coprime to 1440, so the firing phase precesses through
the day by a fixed amount on each recompile, covering different hours across multiple
days deterministically. Unlike jitter, the times are reproducible from the epoch.

Use `--reset-epoch` to advance each inline epoch to the last actual firing time,
keeping the source file current without changing the firing phase.

### Day filters

```
# filter: workday           # Mon–Fri (modifies dow field)
# filter: weekend           # Sat–Sun
# filter: last_dom          # last day of each month
# filter: nth_weekday:2,Mon # second Monday
# filter: every_n_days:5,2026-06-17
# filter: between:2026-06-01,2026-08-31
# filter: none              # clear (also: off)
```

Filters are sticky — they apply to all following compiled lines until overridden.
`workday` and `weekend` modify the dow field directly. The others wrap the command
in a Python guard that exits 0 silently on non-matching days.

### Special dom values

```
18:00  -1      * *  cmd   # last day of month
18:00  first   * monday  cmd  # first Monday
18:00  last    * friday  cmd  # last Friday
```

## Options

| Flag | Description |
|---|---|
| `--src PATH` | Source file (default `~/.crontab.in`) |
| `--dry-run` | Print generated crontab, do not install |
| `-e`, `--edit` | Open source in `$VISUAL`/`$EDITOR` (atomic write), then recompile |
| `--reset-epoch` | Advance each `/Nm` inline-date epoch to the last actual firing, then recompile |
| `--loop` | Compile then loop in-process |
| `--daemon` | Fork+setsid, write PID, then loop |

`--edit` and `--reset-epoch` are daemon-aware: if the daemon is already running it
will recompile automatically (via kqueue); otherwise the compile happens inline.

## Example `~/.crontab.in`

```
PATH=/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin

# tz: America/Los_Angeles
# lat: 33.52  lon: -117.71

# daily summary after sunset
sunset+180  * * *  python3 ~/bin/daily_summary.py >> ~/log/summary.log 2>&1

# hourly during daylight
sunrise-30-sunset+60/1h  * * *  python3 ~/bin/hourly.py >> ~/log/hourly.log 2>&1

# irrational-interval API poll (~9h 11min, drifts through the day)
2026-01-01T00:00/551m  * * *  python3 ~/bin/api_poll.py >> ~/log/poll.log 2>&1

# filter: workday
09:00~5  * * *  python3 ~/bin/morning_standup.py

# filter: none
# tz: local
# recompile daily to keep solar times and interval phases current
03:01~10  * * *  crontabctl >> ~/log/crontabctl.log 2>&1
```
