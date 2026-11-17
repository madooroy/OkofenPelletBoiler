#!/usr/bin/env python3
"""
pellet_watch.py - pellet reorder reminder for an ÖkoFEN Pellematic boiler.

Reads the burner run time (pe1.L_runtime) and burner starts (pe1.L_starts) from
the boiler's local JSON interface, counts burner hours since the last pellet
delivery, writes a status web page, and sends alerts: heads-ups on the way to
the order point (by default at 1/4, 1/2 and 3/4 of it), then at the order point
and the hard stop, with a daily reminder after that until the count is
restarted. It also tracks burner cycling (starts per day, average burn length), outdoor
temperature from the boiler's online weather service (heating degree-days,
burner hours per degree-day), and - advisory only - the boiler setpoint an
outdoor-temperature curve would choose. In --serve mode it can also log the
boiler's temperature and state every minute to a CSV file for analysis.
Nothing is ever written to the boiler.

Usage:
    python3 pellet_watch.py                  check, update the page, alert if due
    python3 pellet_watch.py --status         read-only: print the current status
                                             (sends nothing, changes nothing)
    python3 pellet_watch.py --delivered      pellets just arrived: restart the
                                             count from the boiler's reading now
    python3 pellet_watch.py --delivered 13250
                                             restart from the reading on delivery day
    python3 pellet_watch.py --undo           undo the last restart
    python3 pellet_watch.py --serve          keep running: serve the status page and
                                             its "Pellets delivered" button

Exit codes:
    0  fine, nothing to report
    1  alert on this check (heads-up, order point, hard stop, daily reminder,
       or the very first run), or a restart from the reset button
    2  problem: boiler unreachable, bad settings, damaged state file, page not
       written, or an unexpected error
DSM Task Scheduler can email you whenever the exit code is not 0.
"""

import argparse
import csv
import glob
import threading
import html
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

try:                                     # Python 3.7+
    from http.server import ThreadingHTTPServer as _Server
except ImportError:                      # pragma: no cover
    from http.server import HTTPServer as _Server

try:
    import fcntl                         # file locking (Linux, macOS, Synology)
except ImportError:                      # Windows: runs without a lock
    fcntl = None

# ----------------------------- SETTINGS ------------------------------------
# Put your own values in pellet_watch_config.json next to this script (see
# pellet_watch_config.example.json) rather than editing them here, so a newer
# version of this file can be dropped in without re-entering anything.
BOILER_IP     = "192.168.1.50"   # boiler's address on your LAN
JSON_PORT     = 4321             # touch panel: General > IP Config > JSON Port
JSON_PASSWORD = "xxxx"           # touch panel: General > IP Config > JSON Password

# Burner hours since the last delivery. "Order" sends the reminder; "stop" is
# the last-chance alarm. Winter needs an earlier reminder because the burner
# runs ~8-10 h/day during the 7-10 days a delivery takes (summer ~3-4 h/day).
WINTER_MONTHS = [10, 11, 12, 1, 2, 3, 4]   # October through April
WINTER_ORDER  = 650
WINTER_STOP   = 700
SUMMER_ORDER  = 675
SUMMER_STOP   = 700
FIRST_BASELINE = None            # burner hours at your last delivery, used once
                                 # on the first run; None = start counting now

HTML_PATH  = None                # status page; None = pellets.html next to this
                                 # script, "" = no page
SERVE_PORT = 8765                # port for the page's "Pellets delivered" button
STALE_HOURS = 13                 # page shows a warning if not updated this long

# Heads-up alerts on the way to the order point, as fractions of it
# (0.25 = a quarter of the way). Each is sent once per delivery. [] = none.
MILESTONES = [0.25, 0.5, 0.75]
# After the order point or hard stop: repeat the alert once a day (at the first
# check from this hour on) until the count is restarted. False = alert once.
DAILY_REMINDER = True
REMINDER_HOUR = 8
# Your pellet supplier's ordering page. Added to order alerts (email and push)
# and shown as a button on the status page once it's time to order. "" = none.
ORDER_URL = ""
NTFY_TOPIC = ""                  # optional phone push: pick a hard-to-guess
                                 # topic name and subscribe to it in the ntfy app

TEMP_UNIT = "C"                  # "C" or "F" for temperatures on the page
TEMP_FACTOR = 0.1                # the JSON interface reports 0.1-degree units
WEATHER_UNIT = "C"               # unit of the online weather temperature
                                 # (weather.L_temp); "F" on imperial-unit boilers

# Advisory outdoor reset (nothing is written to the boiler): the boiler
# setpoint a straight-line curve would choose between these two points,
# in degC. Defaults: 74 degC at -10 degC outside, 65 degC at +10 degC outside.
# Check the lowest safe boiler temperature with your installer.
ADVISORY = True
ADVISORY_COLD_OUTDOOR = -10
ADVISORY_COLD_BOILER = 74
ADVISORY_MILD_OUTDOOR = 10
ADVISORY_MILD_BOILER = 65
DEGREE_DAY_BASE = 18.3           # degC (65 degF), for heating degree-days

# Minute-by-minute log (boiler temperature, setpoint, state, modulation,
# outdoor temperature) written by --serve to pellet_watch_log_YYYY-MM.csv.
LOG_SECONDS = 0                  # 0 = off; 60 = once a minute
LOG_KEEP_MONTHS = 3
# ---------------------------------------------------------------------------

SCRIPT_PATH = os.path.abspath(__file__)
HERE = os.path.dirname(SCRIPT_PATH)
ICON_PATH = os.path.join(HERE, "pellets-icon.png")
STATE_PATH = os.path.join(HERE, "pellet_watch_state.json")
BACKUP_PATH = STATE_PATH + ".bak"
LOCK_PATH = os.path.join(HERE, "pellet_watch.lock")
CONFIG_PATH = os.path.join(HERE, "pellet_watch_config.json")

SETTING_TYPES = {
    "BOILER_IP": str, "JSON_PORT": int, "JSON_PASSWORD": str,
    "WINTER_MONTHS": list, "WINTER_ORDER": (int, float), "WINTER_STOP": (int, float),
    "SUMMER_ORDER": (int, float), "SUMMER_STOP": (int, float),
    "FIRST_BASELINE": (int, float, type(None)), "HTML_PATH": (str, type(None)),
    "SERVE_PORT": int, "STALE_HOURS": (int, float), "MILESTONES": list,
    "DAILY_REMINDER": bool, "REMINDER_HOUR": int, "ORDER_URL": str,
    "NTFY_TOPIC": str, "TEMP_UNIT": str, "TEMP_FACTOR": (int, float),
    "WEATHER_UNIT": str,
    "ADVISORY": bool, "ADVISORY_COLD_OUTDOOR": (int, float),
    "ADVISORY_COLD_BOILER": (int, float), "ADVISORY_MILD_OUTDOOR": (int, float),
    "ADVISORY_MILD_BOILER": (int, float), "DEGREE_DAY_BASE": (int, float),
    "LOG_SECONDS": int, "LOG_KEEP_MONTHS": int,
}


class SetupError(Exception):
    """Settings or state that need fixing by hand. Exit code 2."""


def load_config():
    """Apply pellet_watch_config.json over the defaults and check the result."""
    g = globals()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError) as e:
        raise SetupError("Can't read %s: %s" % (CONFIG_PATH, e))
    if not isinstance(cfg, dict):
        raise SetupError("%s must contain a JSON object {...}" % CONFIG_PATH)
    for key, value in cfg.items():
        if key not in SETTING_TYPES:
            print("Warning: unknown setting %r in %s (ignored; check the spelling)"
                  % (key, os.path.basename(CONFIG_PATH)), file=sys.stderr)
            continue
        expected = SETTING_TYPES[key]
        if isinstance(value, bool) and expected is not bool and bool not in (
                expected if isinstance(expected, tuple) else (expected,)):
            raise SetupError("Setting %s has the wrong type: %r" % (key, value))
        if not isinstance(value, expected):
            raise SetupError("Setting %s has the wrong type: %r" % (key, value))
        g[key] = value
    if g["HTML_PATH"] is None:
        g["HTML_PATH"] = os.path.join(HERE, "pellets.html")
    for season in ("WINTER", "SUMMER"):
        order, stop = g[season + "_ORDER"], g[season + "_STOP"]
        if not 0 < order < stop:
            raise SetupError("%s_ORDER (%s) must be above 0 and below %s_STOP (%s)"
                             % (season, order, season, stop))
    for f in g["MILESTONES"]:
        if not isinstance(f, (int, float)) or isinstance(f, bool) or not 0 < f < 1:
            raise SetupError("MILESTONES must be fractions between 0 and 1, e.g. "
                             "[0.25, 0.5, 0.75]; got %r" % (g["MILESTONES"],))
    if not all(isinstance(m, int) and 1 <= m <= 12 for m in g["WINTER_MONTHS"]):
        raise SetupError("WINTER_MONTHS must be month numbers 1-12")
    if g["TEMP_UNIT"] not in ("C", "F"):
        raise SetupError('TEMP_UNIT must be "C" or "F"')
    if g["WEATHER_UNIT"] not in ("C", "F"):
        raise SetupError('WEATHER_UNIT must be "C" or "F"')
    if g["ADVISORY_COLD_OUTDOOR"] >= g["ADVISORY_MILD_OUTDOOR"]:
        raise SetupError("ADVISORY_COLD_OUTDOOR must be below ADVISORY_MILD_OUTDOOR")
    if g["LOG_SECONDS"] and g["LOG_SECONDS"] < 30:
        raise SetupError("LOG_SECONDS must be 0 (off) or at least 30")


# ----------------------------- boiler --------------------------------------

def _number(value):
    if isinstance(value, dict):          # some firmware wraps it as {"val": ...}
        value = value.get("val")
    return float(str(value).replace(",", ""))


def read_boiler_once():
    url = "http://%s:%s/%s/all" % (BOILER_IP, JSON_PORT,
                                   urllib.parse.quote(JSON_PASSWORD, safe=""))
    raw = urllib.request.urlopen(url, timeout=20).read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    data = json.loads(text)
    pe1 = data["pe1"]
    hours = _number(pe1["L_runtime"])
    if not math.isfinite(hours) or hours < 0:
        raise ValueError("boiler reported an invalid burner run time: %r" % hours)
    try:
        starts = _number(pe1["L_starts"])
        if not math.isfinite(starts) or starts < 0:
            starts = None
    except (KeyError, ValueError, TypeError):
        starts = None
    weather = data.get("weather") or {}
    extra = {
        "outdoor": _outdoor(weather.get("L_temp")),
        "boiler": _temp(pe1.get("L_temp_act"), -10, 120),
        "boiler_set": _temp(pe1.get("L_temp_set"), 0, 120),
        "chamber": _temp(pe1.get("L_frt_temp_act"), -10, 1200),
        "modulation": _plain(pe1.get("L_modulation")),
        "state": _plain(pe1.get("L_state")),
        "statetext": _text(pe1.get("L_statetext")),
    }
    return hours, starts, extra


def _plain(value):
    try:
        v = _number(value)
        if not math.isfinite(v):
            return None
        return int(v) if v == int(v) else v
    except (ValueError, TypeError):
        return None


def _temp(value, lo, hi):
    """A temperature in degC from the boiler's 0.1-degree units, or None."""
    v = _plain(value)
    if v is None:
        return None
    v = round(v * TEMP_FACTOR, 1)
    return v if lo <= v <= hi else None


def _outdoor(value):
    """Online-weather temperature in degC, converted if the boiler reports
    it in degF (WEATHER_UNIT)."""
    v = _temp(value, -60, 130)
    if v is not None and WEATHER_UNIT == "F":
        v = round((v - 32) * 5 / 9, 1)
    return v if v is not None and -50 <= v <= 50 else None


def _setpoint(extra):
    """The boiler's current target in degC, or None while it isn't heating
    (the boiler then reports a placeholder such as 8 degC)."""
    v = extra.get("boiler_set")
    return v if v is not None and v >= 40 else None


def _text(value):
    if isinstance(value, dict):
        value = value.get("val")
    return None if value is None else str(value)[:60]


def read_boiler(attempts=3, wait=10):
    """(burner hours, burner starts or None, extra readings dict). Retries,
    because the boiler's JSON server is sometimes briefly busy."""
    for i in range(attempts):
        try:
            return read_boiler_once()
        except Exception as e:           # network, HTTP, JSON or missing field
            error = e
            if i < attempts - 1:
                time.sleep(wait)
    # Never let the password reach a message, the page or an email.
    raise RuntimeError(str(error).replace(JSON_PASSWORD, "***")
                       if JSON_PASSWORD else str(error))


def thresholds():
    """(order_hours, stop_hours, season name) for today's date."""
    if time.localtime().tm_mon in WINTER_MONTHS:
        return WINTER_ORDER, WINTER_STOP, "Winter"
    return SUMMER_ORDER, SUMMER_STOP, "Summer"


# ----------------------------- state ---------------------------------------

class Lock:
    """Exclusive lock so the scheduled check and the reset button never
    overwrite each other's changes."""
    def __enter__(self):
        self.f = open(LOCK_PATH, "a")
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def load_state():
    """The saved state. Returns None if there is none yet (first run).
    A damaged file is never silently replaced: that would restart the count."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise SetupError(
            "The state file %s is damaged (%s). It has been left untouched. "
            "Restore it from %s if that looks right, or delete it and run "
            "--delivered <hours at your last delivery>."
            % (STATE_PATH, e, os.path.basename(BACKUP_PATH)))
    if not isinstance(state, dict) or not _valid_number(state.get("baseline")):
        raise SetupError(
            "The state file %s has no valid delivery reading. It has been left "
            "untouched. Restore it from %s, or run --delivered <hours at your "
            "last delivery>." % (STATE_PATH, os.path.basename(BACKUP_PATH)))
    state.setdefault("history", [])
    state.setdefault("level", "ok")
    return state


def save_state(state):
    tmp = "%s.%d.tmp" % (STATE_PATH, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    if os.path.exists(STATE_PATH):
        try:                             # keep the last good copy
            shutil.copy2(STATE_PATH, BACKUP_PATH)
        except OSError:
            pass
    os.replace(tmp, STATE_PATH)


def _valid_number(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) and x >= 0)


def check_baseline(new, reading):
    """Refuse delivery readings that can't be right (and that would otherwise
    silently disable every alert)."""
    if not _valid_number(new) or new <= 0:
        raise SetupError("%r is not a valid burner-hours reading." % (new,))
    if new > reading + 0.5:
        raise SetupError("%.0f h is higher than the boiler's current reading "
                         "(%.0f h). Check the number." % (new, reading))
    oldest = reading - 2 * max(WINTER_STOP, SUMMER_STOP)
    if new < oldest:
        raise SetupError("%.0f h is more than %d h before the boiler's current "
                         "reading (%.0f h). Check the number."
                         % (new, reading - oldest, reading))


# ----------------------------- alerts --------------------------------------

def push(title, message, click_url=""):
    if not NTFY_TOPIC:
        return
    headers = {"Title": title, "Priority": "high", "Tags": "fire"}
    if click_url:
        headers["Click"] = click_url     # tapping the notification opens it
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + urllib.parse.quote(NTFY_TOPIC, safe=""),
            data=message.encode("utf-8"), headers=headers)
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:               # a failed push must not stop the check
        print("ntfy push failed: %s" % e)


def milestone_marks(order, stop):
    marks = [("%g%%" % round(f * 100, 2), f * order) for f in sorted(set(MILESTONES))]
    return marks + [("order", order), ("stop", stop)]


def pick_alert(state, used, order, stop, level):
    """The alert to send on this check, or None. Updates state["notified"].
    Heads-up milestones, the order point and the hard stop are each sent once
    per delivery; past the order point a daily reminder follows if enabled."""
    reached = [key for key, hours in milestone_marks(order, stop) if used >= hours]
    if "notified" not in state:
        # First run with alert tracking: don't resend what was already due.
        old = {"warn": "order"}.get(state.get("level"), state.get("level"))
        seeded = [k for k in reached if k not in ("order", "stop")]
        if old in ("order", "stop"):
            seeded.append("order")
        if old == "stop":
            seeded.append("stop")
        state["notified"] = seeded
    new = [k for k in reached if k not in state["notified"]]
    state["notified"] += new
    if "order" in state["notified"] and "order" not in new:
        # Heads-ups only make sense before the order point (e.g. after the
        # MILESTONES setting changes): record them without sending.
        new = [k for k in new if k in ("order", "stop")]
    today = time.strftime("%Y-%m-%d")

    if "stop" in new:
        state["last_reminder"] = today
        return ("Pellets OVERDUE - hard stop of %d h reached. Order now; the "
                "hopper may run dry before they arrive." % stop)
    if "order" in new:
        state["last_reminder"] = today
        return "Time to order pellets - order point of %d h reached." % order
    if new:
        return ("Pellets heads-up: %s of the way to the order point "
                "(%.0f of %d h, %.0f h to go)." % (new[-1], used, order, order - used))
    if (DAILY_REMINDER and level in ("order", "stop")
            and state.get("last_reminder") != today
            and time.localtime().tm_hour >= REMINDER_HOUR):
        state["last_reminder"] = today
        if level == "stop":
            return "Reminder: pellets OVERDUE (%.0f of %d h). Order now." % (used, stop)
        return "Reminder: time to order pellets (%.0f h used)." % used
    return None


def current_level(state, used, order, stop):
    level = "stop" if used >= stop else "order" if used >= order else "ok"
    # Once the order alert has gone out it stays "order" until the next
    # delivery, even if the order point moves up (winter -> summer).
    if level == "ok" and "order" in state.get("notified", []):
        level = "order"
    return level


# ----------------------------- page ----------------------------------------

def hours_per_day(history):
    """Average burn rate over the stored readings (up to ~14 days)."""
    if len(history) < 2:
        return None
    (t0, h0), (t1, h1) = history[0][:2], history[-1][:2]
    days = (t1 - t0) / 86400.0
    if days < 0.5 or h1 <= h0:
        return None
    return (h1 - h0) / days


def suggested_setpoint(outdoor):
    """Advisory boiler setpoint (degC) for an outdoor temperature (degC):
    a straight line between the two ADVISORY points, flat beyond them."""
    if outdoor is None or not ADVISORY:
        return None
    x0, y0 = ADVISORY_COLD_OUTDOOR, ADVISORY_COLD_BOILER
    x1, y1 = ADVISORY_MILD_OUTDOOR, ADVISORY_MILD_BOILER
    x = min(max(outdoor, x0), x1)
    return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 1)


def fmt_temp(c, digits=0):
    if c is None:
        return "?"
    if TEMP_UNIT == "F":
        return "%.*f °F" % (digits, c * 9 / 5 + 32)
    return "%.*f °C" % (digits, c)


def degree_days(points):
    """Heating degree-days (degC-days) across readings [t, ..., outdoor_c].
    None if fewer than 2 readings have an outdoor temperature."""
    pts = [p for p in points if len(p) > 3 and p[3] is not None]
    if len(pts) < 2:
        return None
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        dt = (b[0] - a[0]) / 86400.0
        if 0 < dt <= 1.5:                # skip long gaps (e.g. NAS was off)
            total += max(0.0, DEGREE_DAY_BASE - (a[3] + b[3]) / 2.0) * dt
    return total


def cycling_window(cycles, end_time, days=7):
    """Starts per day and average burn minutes for the `days` before end_time.
    None until at least a day of readings is available. Burner hours are
    whole numbers, so short windows are rough."""
    pts = [c for c in cycles if len(c) > 2 and c[2] is not None
           and c[0] <= end_time + 60]
    if len(pts) < 2:
        return None
    last = pts[-1]
    window = [c for c in pts if c[0] >= last[0] - days * 86400]
    first = window[0]
    span = (last[0] - first[0]) / 86400.0
    d_starts = last[2] - first[2]
    d_hours = last[1] - first[1]
    if span < 1.0 or d_starts <= 0 or d_hours < 0:
        return None
    hdd = degree_days(window)
    return {"days": span, "starts_per_day": d_starts / span,
            "avg_burn_min": d_hours * 60.0 / d_starts,
            "hdd": hdd,
            "hours_per_hdd": d_hours / hdd if hdd and hdd >= 1 else None}


def cycling_rows(cycles, now_hours):
    """The burner cycling section, or "" if the boiler doesn't report starts."""
    if not cycles or cycles[-1][2] is None:
        return ""
    starts = cycles[-1][2]
    rows = []
    cur = cycling_window(cycles, cycles[-1][0])
    prev = cycling_window(cycles, cycles[-1][0] - 7 * 86400)
    full_weeks = cur and prev and cur["days"] >= 6 and prev["days"] >= 6

    def was(key, fmt):
        return (" <span class='was'>(week before: %s)</span>" % (fmt % prev[key])
                if full_weeks else "")
    if cur:
        label = "last %d days" % round(cur["days"]) if cur["days"] >= 1.5 else "last day"
        rows.append("<tr><td>Starts per day <span class='was'>(%s)</span></td>"
                    "<td>%.0f%s</td></tr>" % (label, cur["starts_per_day"],
                                              was("starts_per_day", "%.0f")))
        rows.append("<tr><td>Average burn <span class='was'>(%s)</span></td>"
                    "<td>%.0f min%s</td></tr>" % (label, cur["avg_burn_min"],
                                                  was("avg_burn_min", "%.0f min")))
    else:
        rows.append("<tr><td colspan='2' class='was' style='text-align:left'>Recent "
                    "cycling figures appear after a day of readings.</td></tr>")
    if starts > 0:
        rows.append("<tr><td>Average burn (lifetime)</td><td>%.0f min</td></tr>"
                    % (now_hours * 60.0 / starts))
    rows.append("<tr><td>Burner starts (lifetime)</td><td>%s</td></tr>"
                % format(starts, ",.0f"))
    return "<h2>Burner cycling</h2>\n<table>\n%s\n</table>" % "\n".join(rows)


def weather_rows(extra, cycles):
    """Outdoor temperature, degree-days and the advisory setpoint."""
    if not extra:
        return ""
    rows = []
    out = extra.get("outdoor")
    if out is not None:
        rows.append("<tr><td>Outdoor <span class='was'>(online weather)</span></td>"
                    "<td>%s</td></tr>" % fmt_temp(out))
    if extra.get("boiler") is not None:
        rows.append("<tr><td>Boiler water now</td><td>%s</td></tr>"
                    % fmt_temp(extra["boiler"]))
    if _setpoint(extra) is not None:
        rows.append("<tr><td>Boiler setpoint now</td><td>%s</td></tr>"
                    % fmt_temp(_setpoint(extra)))
    sug = suggested_setpoint(out)
    if sug is not None:
        rows.append("<tr><td>Suggested setpoint <span class='was'>(advisory)</span>"
                    "</td><td>%s</td></tr>" % fmt_temp(sug))
    cur = prev = None
    if cycles:
        cur = cycling_window(cycles, cycles[-1][0])
        prev = cycling_window(cycles, cycles[-1][0] - 7 * 86400)
    if cur and cur.get("hdd") is not None and cur["days"] >= 1:
        unit = 1.8 if TEMP_UNIT == "F" else 1.0          # degF-days
        label = "last %d days" % round(cur["days"]) if cur["days"] >= 1.5 else "last day"
        rows.append("<tr><td>Heating degree-days <span class='was'>(%s)</span></td>"
                    "<td>%.0f</td></tr>" % (label, cur["hdd"] * unit))
        if cur.get("hours_per_hdd"):
            before = ""
            if (prev and prev.get("hours_per_hdd") and cur["days"] >= 6
                    and prev["days"] >= 6):
                before = (" <span class='was'>(week before: %.2f)</span>"
                          % (prev["hours_per_hdd"] / unit))
            rows.append("<tr><td>Burner hours per degree-day</td><td>%.2f%s</td></tr>"
                        % (cur["hours_per_hdd"] / unit, before))
    if not rows:
        return ""
    note = ("<p class='hint'>The suggested setpoint is advisory only: nothing is "
            "changed on the boiler.</p>" if sug is not None else "")
    return "<h2>Weather</h2>\n<table>\n%s\n</table>%s" % ("\n".join(rows), note)


def gauge_svg(used, order, stop):
    """Fuel-gauge dial: full (green) on the right, needle moves left to amber
    at the order point and red at the hard stop."""
    top = stop * 1.1
    cx, cy, r = 150.0, 150.0, 115.0

    def pt(hours, radius=r):
        f = min(1.0, max(0.0, hours / top))
        a = math.pi * f
        return cx + radius * math.cos(a), cy - radius * math.sin(a)

    def arc(h0, h1, color):
        x0, y0 = pt(h0)
        x1, y1 = pt(h1)
        return ('<path d="M%.1f %.1f A%.0f %.0f 0 0 0 %.1f %.1f" fill="none" '
                'stroke="%s" stroke-width="30"/>' % (x0, y0, r, r, x1, y1, color))

    nx, ny = pt(used, r - 22)
    tx, ty = pt(stop, r + 24)
    return "".join([
        '<svg viewBox="0 0 300 175" class="gauge" role="img" '
        'aria-label="%.0f of %d hours used">' % (used, stop),
        arc(0, order, "#3a9a3a"), arc(order, stop, "#e0a100"),
        arc(stop, top, "#d23b2a"),
        '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#f2f2f2" '
        'stroke-width="7" stroke-linecap="round"/>' % (cx, cy, nx, ny),
        '<circle cx="%.1f" cy="%.1f" r="11" fill="#f2f2f2"/>' % (cx, cy),
        '<text x="265" y="172" class="tick">Full</text>',
        '<text x="%.1f" y="%.1f" class="tick">%d</text>' % (tx, ty, stop),
        '</svg>'])


def reset_form(now_hours):
    """The "Pellets delivered" button. It posts to this script's --serve mode
    on SERVE_PORT of the same machine, which restarts the count and sends the
    browser back to this page."""
    value = "%.0f" % now_hours if now_hours is not None else ""
    return """
<details class="reset"><summary>Pellets delivered?</summary>
<form method="post" id="resetform"
      onsubmit="return confirm('Restart the count from ' + this.hours.value + ' h?')">
<label>Burner run time at delivery
<input name="hours" type="number" inputmode="numeric" required value="%s"></label>
<input type="hidden" name="back" value="">
<button type="submit">Restart the count</button>
<p class="hint">Today's reading is filled in. Change it if the pellets came on an
earlier day.</p>
</form>
<script>
(function () {
  var f = document.getElementById("resetform");
  f.action = "http://" + location.hostname + ":%d/reset";
  f.back.value = location.href;
})();
</script>
</details>""" % (value, SERVE_PORT)


PAGE_STYLE = """
body{font-family:system-ui,sans-serif;background:#2b2b2b;color:#eee;max-width:26rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.1rem;font-weight:600;color:#9c9}
h2{font-size:1rem;font-weight:600;color:#9c9;margin:1.6rem 0 0}
.was{color:#999;font-size:.85em}
.orderbtn{display:block;text-align:center;margin:.8rem 0;padding:.8rem;border-radius:.5rem;
  background:#d99a00;color:#fff;font-weight:700;font-size:1.1rem;text-decoration:none}
.badge{display:inline-block;padding:.3rem .8rem;border-radius:1rem;font-weight:700;color:#fff}
.big{font-size:3rem;font-weight:700;margin:0 0 .3rem;text-align:center}
.big span{font-size:1rem;font-weight:400;color:#aaa}
.gauge{display:block;width:100%;max-width:20rem;margin:1rem auto 0}
.tick{fill:#aaa;font-size:13px;text-anchor:middle}
table{width:100%;margin-top:1rem;border-collapse:collapse}
td{padding:.45rem 0;border-bottom:1px solid #444}
td:last-child{text-align:right}
.warnbox{background:#5a1f1f;color:#fdd;border-radius:.5rem;padding:.6rem .8rem;margin:.8rem 0}
.foot{color:#888;font-size:.8rem;margin-top:1rem}
.reset{margin-top:1.5rem;background:#333;border-radius:.6rem;padding:.7rem .9rem}
.reset summary{cursor:pointer;font-weight:600;color:#9c9}
.reset label{display:block;margin:.8rem 0 .4rem;color:#ccc}
.reset input{display:block;width:100%;box-sizing:border-box;margin-top:.3rem;padding:.5rem;
  font-size:1.2rem;background:#222;color:#eee;border:1px solid #555;border-radius:.4rem}
.reset button{width:100%;padding:.7rem;font-size:1rem;font-weight:700;border:0;
  border-radius:.4rem;background:#3a9a3a;color:#fff}
.hint{color:#999;font-size:.8rem}
"""


def render_page(now_hours, state, level, error=None, extra=None):
    """The whole status page as a string. With error set, shows it above the
    last known figures (or alone if there are none)."""
    order, stop, season = thresholds()
    level = {"warn": "order"}.get(level, level)          # very old state files
    parts = []
    if error:
        parts.append("<div class='warnbox'>Could not read the boiler: %s</div>"
                     % html.escape(error))
    if now_hours is not None and state and state.get("baseline") is not None:
        baseline = state["baseline"]
        used = max(0.0, now_hours - baseline)
        left = order - used
        colors = {"ok": "#3a9a3a", "order": "#d99a00", "stop": "#c62828"}
        labels = {"ok": "OK", "order": "Order pellets", "stop": "OVERDUE - ORDER NOW"}
        order_button = ""
        if ORDER_URL and level in ("order", "stop"):
            order_button = ('<a class="orderbtn" href="%s" target="_blank" '
                            'rel="noopener">Order pellets</a>'
                            % html.escape(ORDER_URL, quote=True))
        rate = hours_per_day(state.get("history", []))
        eta = ""
        if rate:
            eta = "<tr><td>Burn rate</td><td>%.1f h/day</td></tr>" % rate
            if left > 0:
                days = left / rate
                when = time.strftime("%a %b %d",
                                     time.localtime(time.time() + days * 86400))
                eta += ("<tr><td>Order by</td><td>about %s (%.0f days)</td></tr>"
                        % (when, days))
        deliveries = state.get("deliveries", [])
        delivered_on = (" <span class='was'>(%s)</span>"
                        % time.strftime("%b %d", time.localtime(deliveries[-1]["time"]))
                        if deliveries else "")
        parts.append("""
<div class="badge" style="background:%s">%s</div>
%s
<div class="big">%.0f <span>of %d h</span></div>
%s
<table>
<tr><td>Hours until order point</td><td>%s</td></tr>
<tr><td>Burner run time%s</td><td>%s h</td></tr>
<tr><td>At last delivery%s</td><td>%s h</td></tr>
<tr><td>%s order point</td><td>%s h (%d)</td></tr>
<tr><td>Hard stop</td><td>%s h (%d)</td></tr>
%s
</table>
%s""" % (colors[level], labels[level], gauge_svg(used, order, stop), used, stop,
         order_button,
         "%.0f h" % left if left > 0 else "now",
         " (last known)" if error else " now", format(now_hours, ",.0f"),
         delivered_on, format(baseline, ",.0f"),
         season, format(baseline + order, ",.0f"), order,
         format(baseline + stop, ",.0f"), stop, eta,
         cycling_rows(state.get("cycles", []), now_hours)
         + weather_rows(extra, state.get("cycles", []))))
    checked = time.time()
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<link rel="apple-touch-icon" href="pellets-icon.png"><link rel="icon" href="pellets-icon.png">
<meta name="apple-mobile-web-app-title" content="Pellets">
<title>Pellets</title>
<style>%s</style></head><body>
<h1>Pellet boiler &ndash; hours since last delivery</h1>
<div id="stale" class="warnbox" hidden>This page hasn't been updated for over
%d hours. Check that the scheduled task is still running.</div>
%s
%s
<p class="foot">Checked %s</p>
<script>
if (Date.now() / 1000 - %d > %d * 3600) document.getElementById("stale").hidden = false;
</script>
</body></html>""" % (PAGE_STYLE, STALE_HOURS, "\n".join(parts),
                     reset_form(now_hours) if not error else "",
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(checked)),
                     int(checked), STALE_HOURS)


def write_page(page):
    """Write the page atomically. Returns False if it couldn't be written."""
    if not HTML_PATH:
        return True
    tmp = "%s.%d.tmp" % (HTML_PATH, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp, HTML_PATH)
        return True
    except OSError as e:
        print("Could not write the status page %s: %s" % (HTML_PATH, e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ----------------------------- commands ------------------------------------

def run(delivered=False, new_baseline=None):
    """One check. With delivered=True, first restart the count from
    new_baseline (or from the boiler's current reading when None)."""
    with Lock():
        state = load_state()             # SetupError if damaged
        first_run = state is None
        if first_run:
            state = {"baseline": FIRST_BASELINE, "level": "ok", "history": []}

        try:
            now_hours, starts, extra = read_boiler()
        except Exception as e:
            print("Could not read the boiler: %s" % e)
            if delivered:
                print("The count was NOT restarted. Try again when the boiler "
                      "is reachable.")
            elif not first_run:
                last = state.get("last_reading")
                write_page(render_page(last, state, state.get("level", "ok"),
                                       error=str(e)))
            return 2

        if delivered:
            new = now_hours if new_baseline is None else new_baseline
            check_baseline(new, now_hours)          # SetupError if implausible
            state.setdefault("deliveries", []).append(
                {"time": time.time(), "old_baseline": state.get("baseline"),
                 "new_baseline": new, "reading": now_hours,
                 "old_notified": state.get("notified"),
                 "old_level": state.get("level", "ok"),
                 "old_last_reminder": state.get("last_reminder")})
            state["deliveries"] = state["deliveries"][-50:]
            state["baseline"] = new
            state["level"] = "ok"
            state["history"] = []
            state["notified"] = []
            state.pop("last_reminder", None)
            print("Count restarted. Baseline is now %.0f h; order at %.0f h."
                  % (new, new + thresholds()[0]))
        elif state.get("baseline") is None:
            state["baseline"] = now_hours
        elif first_run:
            check_baseline(state["baseline"], now_hours)

        order, stop, season = thresholds()
        used = now_hours - state["baseline"]
        level = current_level(state, used, order, stop)

        now = time.time()
        state["last_reading"] = now_hours
        state["history"] = [h for h in state.get("history", []) + [[now, now_hours]]
                            if h[0] >= now - 14 * 86400][-400:]
        if starts is not None:           # kept across deliveries for cycling
            state["cycles"] = [c for c in state.get("cycles", [])
                               + [[now, now_hours, starts, extra.get("outdoor")]]
                               if c[0] >= now - 60 * 86400][-1500:]

        summary = ("%.0f burner hours used since the last delivery; %s order point "
                   "is %d h, hard stop %d h. Boiler reads %.0f h."
                   % (used, season.lower(), order, stop, now_hours))
        alert = pick_alert(state, used, order, stop, level)
        state["level"] = level
        save_state(state)
        page_ok = write_page(render_page(now_hours, state, level, extra=extra))

    if first_run and not delivered:
        print("First run: counting from %.0f h. If your last delivery was "
              "earlier, run with --delivered <hours at delivery>." % state["baseline"])
        print(summary)
        return 1
    if alert:
        # Headline first: it is the first line of the DSM Task Scheduler email.
        url = ORDER_URL if level in ("order", "stop") else ""
        print(alert)
        if url:
            print("Order pellets here: %s" % url)
        print(summary)
        push(alert, summary, url)
        return 1
    print(summary)
    if delivered:
        push("Pellet count restarted", summary)
    return 0 if page_ok else 2


def status():
    """Read-only check: prints where things stand, changes nothing."""
    state = load_state()
    if state is None:
        print("No count yet: run pellet_watch.py once (or --delivered <hours>).")
        return 0
    try:
        now_hours, _, extra = read_boiler(attempts=1)
    except Exception as e:
        print("Could not read the boiler: %s" % e)
        return 2
    order, stop, season = thresholds()
    used = now_hours - state["baseline"]
    level = current_level(state, used, order, stop)
    print("%s: %.0f burner hours used since the last delivery (%.0f h). %s order "
          "point %d h, hard stop %d h. Boiler reads %.0f h. Alerts already sent: %s."
          % ({"ok": "OK", "order": "ORDER", "stop": "OVERDUE"}[level], used,
             state["baseline"], season, order, stop, now_hours,
             ", ".join(state.get("notified", [])) or "none"))
    sug = suggested_setpoint(extra.get("outdoor"))
    print("Outdoor (online weather): %s. Boiler water %s, setpoint %s, state %s "
          "(%s), modulation %s. Suggested setpoint (advisory): %s."
          % (fmt_temp(extra.get("outdoor"), 1), fmt_temp(extra.get("boiler"), 1),
             fmt_temp(_setpoint(extra), 1) if _setpoint(extra) is not None
             else "none (not heating)", extra.get("statetext") or "?",
             extra.get("state") if extra.get("state") is not None else "?",
             "?" if extra.get("modulation") is None else "%.0f%%" % extra["modulation"],
             fmt_temp(sug, 1)))
    return 0


def undo():
    """Undo the last restart of the count."""
    with Lock():
        state = load_state()
        deliveries = (state or {}).get("deliveries", [])
        if not deliveries or deliveries[-1].get("old_baseline") is None:
            print("Nothing to undo.")
            return 2
        last = deliveries.pop()
        state["baseline"] = last["old_baseline"]
        state["level"] = last.get("old_level", "ok")
        for key in ("notified", "last_reminder"):
            if last.get("old_" + key) is None:
                state.pop(key, None)
            else:
                state[key] = last["old_" + key]
        save_state(state)
    print("Restart undone: baseline is back to %.0f h. The next check updates "
          "the page." % state["baseline"])
    return 0


# ----------------------------- reset button server -------------------------

class ResetHandler(BaseHTTPRequestHandler):
    timeout = 10                         # drop clients that send nothing

    def _send(self, code, text, location=None):
        data = text.encode("utf-8")
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host(self):
        return (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()

    def do_GET(self):
        # Also serves the status page itself, so no other web server is needed
        # (e.g. on a Raspberry Pi): http://<this machine>:SERVE_PORT/
        path = urllib.parse.urlsplit(self.path).path
        files = {"/": (HTML_PATH, "text/html; charset=utf-8"),
                 "/pellets.html": (HTML_PATH, "text/html; charset=utf-8"),
                 "/pellets-icon.png": (ICON_PATH, "image/png")}
        if path not in files or not files[path][0]:
            return self._send(404, "Not found")
        try:
            with open(files[path][0], "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, "<p>No page yet - run pellet_watch.py once.</p>")
        self.send_response(200)
        self.send_header("Content-Type", files[path][1])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/reset":
            return self._send(404, "Not found")
        # Only accept the form from a page on this same machine.
        host = self._host()
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if value and value != "null":
                if (urllib.parse.urlsplit(value).hostname or "").lower() != host:
                    return self._send(403, "<p>Forbidden.</p>")
                break
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        length = max(0, min(length, 1000))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        back_link = "<a href='javascript:history.back()'>Back</a>"
        try:
            hours = float(form.get("hours", [""])[0].replace(",", ""))
            if not math.isfinite(hours) or hours <= 0:
                raise ValueError
        except ValueError:
            return self._send(400, "<p>Please enter the burner hours as a number. %s</p>"
                              % back_link)
        # Run the current version of the script (not the copy this server
        # loaded at startup), so updates take effect without a restart.
        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT_PATH, "--delivered", "%.0f" % hours],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=150,
                cwd=HERE)
            output = proc.stdout.decode("utf-8", "replace").strip()
            ok = proc.returncode in (0, 1)
        except subprocess.TimeoutExpired:
            output, ok = "Timed out waiting for the boiler.", False
        print(output)
        if not ok:
            return self._send(400, "<p>The count was not restarted.</p><pre>%s</pre>"
                              "<p>%s</p>" % (html.escape(output), back_link))

        back = form.get("back", [""])[0]
        parts = urllib.parse.urlsplit(back)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != host:
            back = "/pellets.html"
        self._send(303, "<p>Count restarted.</p>", location=back)

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))
        sys.stdout.flush()


LOG_FIELDS = ["time", "outdoor_c", "boiler_c", "boiler_set_c", "chamber_c",
              "modulation", "state", "statetext", "burner_hours", "starts",
              "suggested_c"]


def log_once():
    """Append one row of boiler readings to this month's CSV log."""
    try:
        hours, starts, extra = read_boiler(attempts=1)
    except Exception:
        return                           # skip this minute; try again next time
    row = [time.strftime("%Y-%m-%d %H:%M:%S"), extra.get("outdoor"),
           extra.get("boiler"), extra.get("boiler_set"), extra.get("chamber"),
           extra.get("modulation"), extra.get("state"), extra.get("statetext"),
           hours, starts, suggested_setpoint(extra.get("outdoor"))]
    path = os.path.join(HERE, time.strftime("pellet_watch_log_%Y-%m.csv"))
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_FIELDS)
        w.writerow(["" if v is None else v for v in row])
    # Keep only the last LOG_KEEP_MONTHS months.
    logs = sorted(glob.glob(os.path.join(HERE, "pellet_watch_log_????-??.csv")))
    for old in logs[:-max(1, LOG_KEEP_MONTHS)]:
        try:
            os.remove(old)
        except OSError:
            pass


def logger_loop():
    while True:
        start = time.time()
        try:
            log_once()
        except Exception as e:           # never let logging stop the server
            print("Log error: %s" % e)
        time.sleep(max(1.0, LOG_SECONDS - (time.time() - start)))


def serve():
    if LOG_SECONDS:
        threading.Thread(target=logger_loop, daemon=True).start()
        print("Logging boiler readings every %d s to %s" % (
            LOG_SECONDS, os.path.join(HERE, "pellet_watch_log_YYYY-MM.csv")))
    server = _Server(("", SERVE_PORT), ResetHandler)
    server.daemon_threads = True
    print("Pellet watch: page and reset button on port %d" % SERVE_PORT)
    sys.stdout.flush()
    server.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Pellet reorder reminder for an ÖkoFEN Pellematic boiler.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true",
                   help="print the current status without changing anything")
    g.add_argument("--delivered", nargs="?", const="now", metavar="HOURS",
                   help="restart the count (from HOURS, or the current reading)")
    g.add_argument("--undo", action="store_true", help="undo the last restart")
    g.add_argument("--serve", action="store_true",
                   help="serve the status page and its reset button")
    args = p.parse_args(argv)
    try:
        load_config()
        if args.serve:
            serve()
            return 0
        if args.status:
            return status()
        if args.undo:
            return undo()
        if args.delivered is not None:
            if args.delivered == "now":
                return run(True, None)
            try:
                value = float(args.delivered.replace(",", ""))
            except ValueError:
                raise SetupError("%r is not a number." % args.delivered)
            return run(True, value)
        return run()
    except SetupError as e:
        print("Problem: %s" % e)
        return 2
    except KeyboardInterrupt:
        return 2
    except Exception:
        print("Unexpected error (please report it):")
        traceback.print_exc(file=sys.stdout)
        return 2


if __name__ == "__main__":
    sys.exit(main())
