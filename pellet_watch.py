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
    python3 pellet_watch.py --test-notify    send a test email / ntfy push
    python3 pellet_watch.py --ordered        pellets ordered: pause the daily
                                             reminders until the delivery
    python3 pellet_watch.py --ecobee-history 2025-11-01 2026-04-30
                                             one-off: save past ecobee records

Exit codes:
    0  fine, nothing to report - or an alert that the script emailed itself
       (SMTP_USER set and the email went out)
    1  alert on this check that was NOT emailed by the script (heads-up, order
       point, hard stop, daily reminder, or the very first run)
    2  problem: boiler unreachable, bad settings, damaged state file, page not
       written, or an unexpected error
DSM Task Scheduler can email you whenever the exit code is not 0; with
SMTP_USER set, that DSM email then only comes for real problems.
"""

import argparse
import calendar
import csv
import glob
import ipaddress
import socket
import threading
import html
import json
import math
import os
import shutil
import smtplib
import ssl
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
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
ORDERED_QUIET_DAYS = 14          # after "Pellets ordered", pause the daily
                                 # reminders this long (then they resume, in
                                 # case the delivery was never recorded)
REMINDER_HOUR = 8
# Your pellet supplier's ordering page. Added to order alerts (email and push)
# and shown as a button on the status page once it's time to order. "" = none.
ORDER_URL = ""
NTFY_TOPIC = ""                  # optional phone push: pick a hard-to-guess
                                 # topic name and subscribe to it in the ntfy app
NTFY_SERVER = "https://ntfy.sh"

# Optional: the script emails alerts itself, with a plain subject such as
# "Time to order pellets". Needs an SMTP account that accepts an app password
# (Yahoo, Gmail; NOT Outlook.com, which no longer allows passwords for SMTP).
# When an alert email is sent, the check exits 0, so the DSM Task Scheduler
# email is left for real problems only (exit code 2).
SMTP_HOST = "smtp.mail.yahoo.com"
SMTP_PORT = 465                  # 465 = SSL; 587 = STARTTLS
SMTP_USER = ""                   # full address of the sending account; "" = off
SMTP_PASSWORD = ""               # an app password, not the account password
MAIL_TO = []                     # recipients; [] = send to SMTP_USER
PAGE_URL = ""                    # status page address, linked in alerts
ALLOWED_HOSTS = []               # extra names the page server may be reached
                                 # by (IPs, localhost, this machine's name and
                                 # the PAGE_URL host are always allowed)

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

# Optional setpoint hint: a push (or email if no ntfy) when the advisory
# setpoint is at least SETPOINT_HINT_DIFF away from the boiler's current one.
# It looks ahead: the colder of the boiler's online forecast for the coming day
# and the average of the last SETPOINT_HINT_DAYS days, so it suggests raising
# the setpoint before a cold snap and lowering it only once it has been mild
# and is forecast to stay mild. You change it yourself; nothing is written to
# the boiler. At most one hint per SETPOINT_HINT_DAYS days, and none again
# until the suggestion changes.
SETPOINT_HINT = False
SETPOINT_HINT_DIFF = 2.8         # K (2.8 K = 5 degF)
SETPOINT_HINT_DAYS = 1           # days of past outdoor temperature to average
SETPOINT_HINT_MIN = None         # degC floor for hints, e.g. 68.3 (155 degF)
                                 # so the hot-water tank still reheats well

# Minute-by-minute log (boiler temperature, setpoint, state, modulation,
# outdoor temperature) written by --serve to pellet_watch_log_YYYY-MM.csv.
LOG_SECONDS = 0                  # 0 = off; 60 = once a minute
LOG_KEEP_MONTHS = 3
# Boiler error alert: email + push when the boiler reports an error state
# (e.g. [5043] vacuum system) for this many minutes. Checked every minute by
# the --serve logger (LOG_SECONDS) and at every scheduled check.
ERROR_ALERT = True
ERROR_ALERT_MINUTES = 3

# Optional ecobee heat dump (trial). After a burn, if the boiler water climbs
# past ECOBEE_DUMP_START_C with no zone calling, raise one ecobee's setpoint by
# ECOBEE_DUMP_BUMP_F for at most ECOBEE_DUMP_MINUTES: its zone valve opens, the
# pump and air-handler fan run, and the trapped heat goes into the house. The
# hold is released once the water is down to ECOBEE_DUMP_STOP_C. Needs the
# --serve logger and pellet_watch_ecobee_token.json from tools/ecobee_login.py.
ECOBEE_DUMP = False
ECOBEE_DUMP_DRY_RUN = False      # True = only log what it would do
ECOBEE_DUMP_THERMOSTAT = ""      # thermostat name (required when ECOBEE_DUMP is on)
ECOBEE_DUMP_START_C = 82.8       # 181 degF
ECOBEE_DUMP_STOP_C = 76.7        # 170 degF
ECOBEE_DUMP_MINUTES = 12         # the hold ends itself after this
ECOBEE_DUMP_BUMP_F = 3
ECOBEE_DUMP_MAX_PER_DAY = 10
ECOBEE_DUMP_NOTIFY = True        # a quiet (low-priority) push after each dump

DATA_DIR = ""                    # where the CSV logs go: "" = next to the script,
                                 # or a folder name/path, e.g. "data"

# Optional: ecobee thermostat heat calls via beestat.io (free). With an API key
# (beestat settings > Manage API Key), each scheduled check saves the ecobees'
# 5-minute runtime records to pellet_watch_ecobee_YYYY-MM.csv, to match
# against the boiler log. "" = off.
BEESTAT_API_KEY = ""
BEESTAT_URL = "https://api.beestat.io/"
# ---------------------------------------------------------------------------

SCRIPT_PATH = os.path.abspath(__file__)
HERE = os.path.dirname(SCRIPT_PATH)
ICON_PATH = os.path.join(HERE, "pellets-icon.png")
STATE_PATH = os.path.join(HERE, "pellet_watch_state.json")
BACKUP_PATH = STATE_PATH + ".bak"
BEESTAT_STATE_PATH = os.path.join(HERE, "pellet_watch_beestat.json")
ECOBEE_TOKEN_PATH = os.path.join(HERE, "pellet_watch_ecobee_token.json")
LOCK_PATH = os.path.join(HERE, "pellet_watch.lock")
CONFIG_PATH = os.path.join(HERE, "pellet_watch_config.json")
DATA_PATH = HERE                 # resolved from DATA_DIR by load_config()

SETTING_TYPES = {
    "BOILER_IP": str, "JSON_PORT": int, "JSON_PASSWORD": str,
    "WINTER_MONTHS": list, "WINTER_ORDER": (int, float), "WINTER_STOP": (int, float),
    "SUMMER_ORDER": (int, float), "SUMMER_STOP": (int, float),
    "FIRST_BASELINE": (int, float, type(None)), "HTML_PATH": (str, type(None)),
    "SERVE_PORT": int, "STALE_HOURS": (int, float), "MILESTONES": list,
    "DAILY_REMINDER": bool, "REMINDER_HOUR": int, "ORDER_URL": str,
    "ORDERED_QUIET_DAYS": (int, float),
    "NTFY_TOPIC": str, "NTFY_SERVER": str, "SMTP_HOST": str, "SMTP_PORT": int,
    "SMTP_USER": str, "SMTP_PASSWORD": str, "MAIL_TO": (list, str), "PAGE_URL": str, "ALLOWED_HOSTS": list,
    "TEMP_UNIT": str, "TEMP_FACTOR": (int, float),
    "WEATHER_UNIT": str,
    "ADVISORY": bool, "ADVISORY_COLD_OUTDOOR": (int, float),
    "ADVISORY_COLD_BOILER": (int, float), "ADVISORY_MILD_OUTDOOR": (int, float),
    "ADVISORY_MILD_BOILER": (int, float), "DEGREE_DAY_BASE": (int, float),
    "SETPOINT_HINT": bool, "SETPOINT_HINT_DIFF": (int, float),
    "SETPOINT_HINT_DAYS": (int, float), "SETPOINT_HINT_MIN": (int, float, type(None)),
    "LOG_SECONDS": int, "LOG_KEEP_MONTHS": int, "DATA_DIR": str,
    "ERROR_ALERT": bool, "ERROR_ALERT_MINUTES": (int, float),
    "ECOBEE_DUMP": bool, "ECOBEE_DUMP_DRY_RUN": bool, "ECOBEE_DUMP_THERMOSTAT": str,
    "ECOBEE_DUMP_START_C": (int, float), "ECOBEE_DUMP_STOP_C": (int, float),
    "ECOBEE_DUMP_MINUTES": (int, float), "ECOBEE_DUMP_BUMP_F": (int, float),
    "ECOBEE_DUMP_MAX_PER_DAY": int, "ECOBEE_DUMP_NOTIFY": bool,
    "BEESTAT_API_KEY": str,
    "BEESTAT_URL": str,
}


class SetupError(Exception):
    """Settings or state that need fixing by hand. Exit code 2."""


def _move_log(old, new):
    """Move a CSV log into DATA_DIR. If the target exists (e.g. a server
    started before DATA_DIR was set kept writing the old file), append the
    rows instead of dropping them."""
    try:
        if not os.path.exists(new):
            shutil.move(old, new)        # works across shared folders too
            return
        with open(old, newline="", encoding="utf-8") as src:
            lines = src.readlines()[1:]  # skip the header
        with open(new, "a", newline="", encoding="utf-8") as dst:
            dst.writelines(lines)
        os.remove(old)
    except OSError as e:
        print("Could not move %s to %s: %s" % (old, new, e))


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
    g["DATA_PATH"] = os.path.join(HERE, g["DATA_DIR"]) if g["DATA_DIR"] else HERE
    try:
        os.makedirs(g["DATA_PATH"], exist_ok=True)
    except OSError as e:
        raise SetupError("Can't create DATA_DIR %s: %s" % (g["DATA_PATH"], e))
    if os.path.realpath(g["DATA_PATH"]) != os.path.realpath(HERE):
        # First run with DATA_DIR set: move existing CSV logs into it.
        for pattern in ("pellet_watch_log_????-??.csv",
                        "pellet_watch_ecobee_????-??.csv",
                        "pellet_watch_ecobee_history_????-??.csv"):
            for old in glob.glob(os.path.join(HERE, pattern)):
                _move_log(old, os.path.join(g["DATA_PATH"], os.path.basename(old)))
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
    if isinstance(g["MAIL_TO"], str):
        g["MAIL_TO"] = [g["MAIL_TO"]] if g["MAIL_TO"] else []
    if not all(isinstance(a, str) and "@" in a for a in g["MAIL_TO"]):
        raise SetupError("MAIL_TO must be an email address or a list of them")
    if g["SMTP_USER"] and not g["SMTP_PASSWORD"]:
        raise SetupError("SMTP_USER is set but SMTP_PASSWORD is empty")
    if not g["ECOBEE_DUMP_STOP_C"] < g["ECOBEE_DUMP_START_C"]:
        raise SetupError("ECOBEE_DUMP_STOP_C must be below ECOBEE_DUMP_START_C")
    if g["ECOBEE_DUMP_STOP_C"] < 70:     # stay well above the ~64 degC restart
        raise SetupError("ECOBEE_DUMP_STOP_C must be at least 70 (degC)")
    if g["ECOBEE_DUMP"] and not g["ECOBEE_DUMP_THERMOSTAT"]:
        raise SetupError("ECOBEE_DUMP needs ECOBEE_DUMP_THERMOSTAT (the thermostat's "
                         "name; --ecobee-status lists them)")
    if not 0 < g["ECOBEE_DUMP_MINUTES"] <= 30 or not 0 < g["ECOBEE_DUMP_BUMP_F"] <= 5:
        raise SetupError("ECOBEE_DUMP_MINUTES must be 1-30 and ECOBEE_DUMP_BUMP_F 1-5")
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
        "forecast": _forecast(weather.get("L_forecast_temp"),
                              _outdoor(weather.get("L_temp"))),
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


def advised_setpoint(outdoor):
    """(degC, note) for display: the advisory curve, but never below
    SETPOINT_HINT_MIN, so the page and --status agree with the hints."""
    sug = suggested_setpoint(outdoor)
    if sug is None or SETPOINT_HINT_MIN is None or sug >= SETPOINT_HINT_MIN:
        return sug, ""
    return SETPOINT_HINT_MIN, " (your minimum)"


def _forecast(value, outdoor):
    """The coming day's forecast average in degC. On some boilers it comes in
    degC even when the current temperature comes in degF (Marc's: 3.7 for a
    39 degF day), so take whichever reading of it is nearer the current
    outdoor temperature."""
    v = _temp(value, -60, 130)
    if v is None:
        return None
    as_c, as_f = v, round((v - 32) * 5 / 9, 1)
    if outdoor is None:
        pick = as_c if WEATHER_UNIT == "C" else as_f
    else:
        pick = as_c if abs(as_c - outdoor) <= abs(as_f - outdoor) else as_f
    return pick if -50 <= pick <= 50 else None


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
        f.flush()
        os.fsync(f.fileno())             # on disk before the rename
    if os.path.exists(STATE_PATH):
        try:                             # keep the last good copy
            btmp = BACKUP_PATH + ".tmp"
            shutil.copy2(STATE_PATH, btmp)
            with open(btmp, "rb") as f:
                os.fsync(f.fileno())
            os.replace(btmp, BACKUP_PATH)
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

def push(title, message, click_url="", priority="high"):
    """Phone push via ntfy. Returns True if sent, False if failed, None if off."""
    if not NTFY_TOPIC:
        return None
    # The title goes in the URL, UTF-8 encoded: as an HTTP header a "°" arrives
    # garbled ("155 �F").
    headers = {"Priority": priority, "Tags": "fire"}
    if click_url:
        headers["Click"] = click_url     # tapping the notification opens it
    if PAGE_URL:
        headers["Actions"] = "view, Status page, %s" % PAGE_URL
    try:
        req = urllib.request.Request(
            NTFY_SERVER.rstrip("/") + "/" + urllib.parse.quote(NTFY_TOPIC, safe="")
            + "?" + urllib.parse.urlencode({"title": title}),
            data=message.encode("utf-8"), headers=headers)
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:               # a failed push must not stop the check
        print("ntfy push failed: %s" % e)
        return False


def send_mail(subject, body, html_body=None):
    """Email via SMTP. Returns True if sent, False if failed, None if off."""
    if not SMTP_USER:
        return None
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "Pellet watch <%s>" % SMTP_USER
    msg["To"] = ", ".join(MAIL_TO or [SMTP_USER])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=SMTP_USER.split("@")[-1])
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30,
                                      context=context)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            server.starttls(context=context)
        with server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:               # report, never leak the password
        text = str(e)
        if SMTP_PASSWORD:
            text = text.replace(SMTP_PASSWORD, "***")
        print("Email failed: %s: %s" % (type(e).__name__, text))
        return False


def alert_html(alert, summary, url):
    """HTML email body: the alert, link buttons, then the summary in grey."""
    e = html.escape
    button = ('<a href="%s" style="display:inline-block;padding:10px 18px;'
              'margin:0 8px 8px 0;border-radius:6px;background:%s;color:#ffffff;'
              'text-decoration:none;font-weight:bold">%s</a>')
    links = ""
    if url:
        links += button % (e(url, quote=True), "#c0392b", "Order pellets")
    if PAGE_URL:
        links += button % (e(PAGE_URL, quote=True), "#2e7d4f", "Status page")
    return ('<!DOCTYPE html><html><body style="font-family:Arial,Helvetica,sans-serif;'
            'font-size:15px;line-height:1.5;color:#222">'
            '<p style="font-size:17px;font-weight:bold;margin:0 0 14px">%s</p>'
            '%s<p style="color:#666;font-size:13px;margin:10px 0 0">%s</p>'
            '</body></html>') % (e(alert), "<p>%s</p>" % links if links else "",
                                 e(summary))


def alert_text(alert, summary, url):
    """Plain email body / push text for an alert."""
    lines = [alert, ""]
    if url:
        lines.append("Order pellets: %s" % url)
    if PAGE_URL:
        lines.append("Status page: %s" % PAGE_URL)
    if url or PAGE_URL:
        lines.append("")
    lines.append(summary)
    return "\n".join(lines)


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

    ordered = state.get("ordered")
    if "stop" in new:
        state["last_reminder"] = today
        if ordered:                      # always sent, even after ordering
            return ("Pellets OVERDUE - hard stop of %d h reached. Ordered on %s "
                    "but not delivered yet: check on the delivery."
                    % (stop, time.strftime("%b %d", time.localtime(ordered))))
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
        if ordered:
            days = (time.time() - ordered) / 86400
            when = time.strftime("%b %d", time.localtime(ordered))
            if level == "stop":          # past the hard stop: never quiet
                state["last_reminder"] = today
                return ("Reminder: pellets OVERDUE (%.0f of %d h). Ordered %s but "
                        "no delivery recorded: check on the delivery."
                        % (used, stop, when))
            if days < ORDERED_QUIET_DAYS:
                return None              # ordered: no daily nagging
            state["last_reminder"] = today
            return ("Reminder: pellets ordered %s (%.0f days ago) but no delivery "
                    "recorded (%.0f h used). Press \"Pellets delivered?\" on the "
                    "status page once they arrive." % (when, days, used))
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

OUTAGE_GAP_S = 30 * 60              # no readings this long = boiler unreachable
OUTAGE_BACK_S = 10 * 60             # this long burning again = outage over
OUTAGE_MIN_S = 30 * 60              # shorter stops aren't worth leaving out


def find_outages(since):
    """Periods the boiler wasn't heating normally, from the minute logs:
    from the first error reading (state 11), or the last reading before 30+
    minutes without any, until it has burned steadily (Powerburn) for 10
    minutes. Returns [[t0, t1, hours0, hours1, starts0, starts1], ...]; an
    outage still going ends at the latest reading. [] without minute logs."""
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA_PATH, "pellet_watch_log_????-??.csv")))[-3:]:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        t = time.mktime(time.strptime(r["time"], "%Y-%m-%d %H:%M:%S"))
                        if t < since:
                            continue
                        rows.append((t, _plain(r.get("state")),
                                     float(r["burner_hours"]), float(r["starts"])))
                    except (KeyError, TypeError, ValueError):
                        continue
        except OSError:
            continue
    rows.sort()
    out, cur, burn, prev = [], None, None, None
    for row in rows:
        t, state = row[0], row[1]
        if cur is None:
            if prev and t - prev[0] >= OUTAGE_GAP_S:
                cur = list(prev)
            elif state == 11:
                cur = list(row)
            if cur is not None:
                burn = None
        if cur is not None:
            if state == 4:
                burn = burn or row
                if t - burn[0] >= OUTAGE_BACK_S:
                    out.append([cur[0], burn[0], cur[2], burn[2], cur[3], burn[3]])
                    cur = burn = None
            else:
                burn = None
        prev = row
    if cur is not None and prev:
        out.append([cur[0], prev[0], cur[2], prev[2], cur[3], prev[3]])
    return [o for o in out if o[1] - o[0] >= OUTAGE_MIN_S]


def outage_share(t0, t1, outages):
    """(seconds, burner hours, starts) of the outages inside t0..t1; a partly
    covered outage counts in proportion to the time inside."""
    secs = hours = starts = 0.0
    for o in outages or []:
        a, b = max(t0, o[0]), min(t1, o[1])
        if b <= a:
            continue
        frac = (b - a) / (o[1] - o[0]) if o[1] > o[0] else 1.0
        secs += b - a
        hours += (o[3] - o[2]) * frac
        starts += (o[5] - o[4]) * frac
    return secs, hours, starts


def hours_per_day(history, outages=None):
    """Average burn rate over the stored readings (up to ~14 days), leaving
    out boiler outages."""
    if len(history) < 2:
        return None
    (t0, h0), (t1, h1) = history[0][:2], history[-1][:2]
    secs, oh, _ = outage_share(t0, t1, outages)
    days = (t1 - t0 - secs) / 86400.0
    if days < 0.5 or h1 - h0 - oh <= 0:
        return None
    return (h1 - h0 - oh) / days


def suggested_setpoint(outdoor):
    """Advisory boiler setpoint (degC) for an outdoor temperature (degC):
    a straight line between the two ADVISORY points, flat beyond them."""
    if outdoor is None or not ADVISORY:
        return None
    x0, y0 = ADVISORY_COLD_OUTDOOR, ADVISORY_COLD_BOILER
    x1, y1 = ADVISORY_MILD_OUTDOOR, ADVISORY_MILD_BOILER
    x = min(max(outdoor, x0), x1)
    return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 1)


def setpoint_hint(state, extra, now=None):
    """Return (title, message) when the boiler setpoint is worth changing by
    hand, else None. Updates state["last_setpoint"] / state["setpoint_hint"]."""
    now = now or time.time()
    current = _setpoint(extra)
    if current is not None:              # only known while the boiler heats
        state["last_setpoint"] = current
    current = state.get("last_setpoint")
    if not (SETPOINT_HINT and ADVISORY) or current is None:
        return None
    days = SETPOINT_HINT_DAYS
    pts = [c[3] for c in state.get("cycles", [])
           if c[0] >= now - days * 86400 and len(c) > 3 and c[3] is not None]
    if len(pts) < max(3, int(days * 4 * 0.75)):   # ~4 checks a day
        return None
    avg = sum(pts) / len(pts)
    forecast = extra.get("forecast") if extra else None
    basis = avg if forecast is None else min(avg, forecast)
    target = suggested_setpoint(basis)
    # Compare in the unit shown, after rounding, so 165 vs 160 degF counts as
    # exactly 5 degF however the boiler rounds its degC value.
    if TEMP_UNIT == "F":                 # 5 degF steps; floor rounded up
        tgt = round((target * 9 / 5 + 32) / 5) * 5
        if SETPOINT_HINT_MIN is not None:
            tgt = max(tgt, math.ceil((SETPOINT_HINT_MIN * 9 / 5 + 32) / 5 - 1e-9) * 5)
        cur = round(current * 9 / 5 + 32)
        far = abs(tgt - cur) >= round(SETPOINT_HINT_DIFF * 9 / 5)
        target = round((tgt - 32) * 5 / 9, 1)
    else:                                # whole degC; floor rounded up
        tgt = round(target)
        if SETPOINT_HINT_MIN is not None:
            tgt = max(tgt, math.ceil(SETPOINT_HINT_MIN - 1e-9))
        far = abs(tgt - round(current)) >= round(SETPOINT_HINT_DIFF)
        target = float(tgt)
    last = state.get("setpoint_hint") or {}
    if not far:
        state.pop("setpoint_hint", None)     # in line now: allow future hints
        return None
    if last.get("target") == target or now - last.get("time", 0) < days * 86400:
        return None
    state["setpoint_hint"] = {"target": target, "time": now}   # undone if unsent
    word = "Milder" if target < current else "Colder"
    span = "day" if days == 1 else "%g days" % days
    if forecast is None:
        why = "outdoor has averaged %s over the last %s" % (fmt_temp(avg), span)
    else:
        why = ("the forecast for the coming day averages %s; the last %s "
               "averaged %s" % (fmt_temp(forecast), span, fmt_temp(avg)))
    title = "Boiler setpoint: consider %s" % fmt_temp(target)
    msg = ("%s weather: %s. The advisory curve suggests about %s (%.1f °C); the "
           "boiler is set to %s (%.1f °C). To change it: myPelletronic > "
           "Pellematic 1 > Setting > Control Temperature > %.1f. Advisory only; "
           "nothing was changed."
           % (word, why, fmt_temp(target), target, fmt_temp(current), current, target))
    return title, msg


def fmt_temp(c, digits=0):
    if c is None:
        return "?"
    if TEMP_UNIT == "F":
        return "%.*f °F" % (digits, c * 9 / 5 + 32)
    return "%.*f °C" % (digits, c)


def degree_days(points, outages=None):
    """Heating degree-days (degC-days) across readings [t, ..., outdoor_c],
    leaving out boiler outages. None if fewer than 2 readings have an
    outdoor temperature."""
    pts = [p for p in points if len(p) > 3 and p[3] is not None]
    if len(pts) < 2:
        return None
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        dt = (b[0] - a[0]) / 86400.0
        if 0 < dt <= 1.5:                # skip long gaps (e.g. NAS was off)
            out = outage_share(a[0], b[0], outages)[0] / 86400.0
            total += max(0.0, DEGREE_DAY_BASE - (a[3] + b[3]) / 2.0) * (dt - out)
    return total


def cycling_window(cycles, end_time, days=7, outages=None):
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
    secs, oh, os_ = outage_share(first[0], last[0], outages)
    span = (last[0] - first[0] - secs) / 86400.0
    d_starts = last[2] - first[2] - os_
    d_hours = last[1] - first[1] - oh
    if span < 1.0 or d_starts <= 0 or d_hours < 0:
        return None
    hdd = degree_days(window, outages)
    return {"days": span, "calendar_days": (last[0] - first[0]) / 86400.0,
            "excluded_h": secs / 3600.0,
            "starts_per_day": d_starts / span,
            "avg_burn_min": d_hours * 60.0 / d_starts,
            "hdd": hdd,
            "hours_per_hdd": d_hours / hdd if hdd and hdd >= 1 else None}


def cycling_rows(cycles, now_hours, outages=None):
    """The burner cycling section, or "" if the boiler doesn't report starts."""
    if not cycles or cycles[-1][2] is None:
        return ""
    starts = cycles[-1][2]
    rows = []
    cur = cycling_window(cycles, cycles[-1][0], outages=outages)
    prev = cycling_window(cycles, cycles[-1][0] - 7 * 86400, outages=outages)
    full_weeks = cur and prev and cur["days"] >= 5 and prev["days"] >= 5

    def was(key, fmt):
        return (" <span class='was'>(week before: %s)</span>" % (fmt % prev[key])
                if full_weeks else "")
    if cur:
        label = ("last %d days" % round(cur["calendar_days"])
                 if cur["calendar_days"] >= 1.5 else "last day")
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
    if cur and cur.get("excluded_h", 0) >= 0.5:
        n = sum(1 for o in outages or [] if o[1] > cycles[-1][0] - 7 * 86400)
        rows.append("<tr><td colspan='2' class='was' style='text-align:left'>"
                    "Recent figures leave out %d boiler outage%s (%.0f h) and "
                    "the restarts during %s.</td></tr>"
                    % (n, "" if n == 1 else "s", cur["excluded_h"],
                       "it" if n == 1 else "them"))
    return "<h2>Burner cycling</h2>\n<table>\n%s\n</table>" % "\n".join(rows)


def weather_rows(extra, cycles, outages=None):
    """Outdoor temperature, degree-days and the advisory setpoint."""
    if not extra:
        return ""
    rows = []
    out = extra.get("outdoor")
    if out is not None:
        rows.append("<tr><td>Outdoor <span class='was'>(online weather)</span></td>"
                    "<td>%s</td></tr>" % fmt_temp(out))
    if extra.get("forecast") is not None:
        rows.append("<tr><td>Forecast <span class='was'>(coming day, average)</span></td>"
                    "<td>%s</td></tr>" % fmt_temp(extra["forecast"]))
    if extra.get("boiler") is not None:
        rows.append("<tr><td>Boiler water now</td><td>%s</td></tr>"
                    % fmt_temp(extra["boiler"]))
    if _setpoint(extra) is not None:
        rows.append("<tr><td>Boiler setpoint now</td><td>%s</td></tr>"
                    % fmt_temp(_setpoint(extra)))
    sug, note = advised_setpoint(out)
    if sug is not None:
        rows.append("<tr><td>Suggested setpoint <span class='was'>(advisory)</span>"
                    "</td><td>%s%s</td></tr>" % (fmt_temp(sug), note))
    cur = prev = None
    if cycles:
        cur = cycling_window(cycles, cycles[-1][0], outages=outages)
        prev = cycling_window(cycles, cycles[-1][0] - 7 * 86400, outages=outages)
    if cur and cur.get("hdd") is not None and cur["days"] >= 1:
        unit = 1.8 if TEMP_UNIT == "F" else 1.0          # degF-days
        label = ("last %d days" % round(cur["calendar_days"])
                 if cur["calendar_days"] >= 1.5 else "last day")
        rows.append("<tr><td>Heating degree-days <span class='was'>(%s)</span></td>"
                    "<td>%.0f</td></tr>" % (label, cur["hdd"] * unit))
        if cur.get("hours_per_hdd"):
            before = ""
            if (prev and prev.get("hours_per_hdd") and cur["days"] >= 5
                    and prev["days"] >= 5):
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


def ordered_form(state, level):
    """"Pellets ordered?" button (with a confirmation) past the order point,
    or the "ordered" note with a button to take it back."""
    if level not in ("order", "stop"):
        return ""
    ordered = state.get("ordered")
    if ordered:
        when = time.strftime("%b %d", time.localtime(ordered))
        body = ('<p class="ordered">Ordered %s &ndash; daily reminders paused until '
                'delivery.</p>'
                '<form method="post" class="ordform" onsubmit="return confirm('
                "'Resume the daily order reminders?')\">"
                '<input type="hidden" name="action" value="cancel">'
                '<input type="hidden" name="back" value="">'
                '<button type="submit" class="small">Not ordered after all</button>'
                '</form>' % when)
    else:
        body = ('<form method="post" class="ordform" onsubmit="return confirm('
                "'Pellets ordered? This pauses the daily reminders until the "
                "delivery is recorded. The hard-stop alert is still sent.')\">"
                '<input type="hidden" name="action" value="yes">'
                '<input type="hidden" name="back" value="">'
                '<button type="submit">I\'ve ordered pellets</button></form>')
    return body + """
<script>
(function () {
  var fs = document.querySelectorAll("form.ordform");
  for (var i = 0; i < fs.length; i++) {
    fs[i].action = "http://" + location.hostname + ":%d/ordered";
    fs[i].back.value = location.href;
  }
})();
</script>""" % SERVE_PORT


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
.ordered{text-align:center;color:#e0c060;margin:.4rem 0}
form.ordform{text-align:center;margin:.4rem 0 .8rem}
form.ordform button{padding:.6rem 1rem;border-radius:.5rem;border:0;background:#555;color:#fff;font-size:1rem}
form.ordform button.small{padding:.3rem .7rem;font-size:.85rem;background:#444;color:#ccc}
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
        if state.get("ordered") and level in ("order", "stop"):
            labels = dict(labels, order="Ordered", stop="OVERDUE - ordered")
        order_button = ""
        if ORDER_URL and level in ("order", "stop") and not state.get("ordered"):
            order_button = ('<a class="orderbtn" href="%s" target="_blank" '
                            'rel="noopener">Order pellets</a>'
                            % html.escape(ORDER_URL, quote=True))
        try:
            outages = find_outages(time.time() - 22 * 86400)
        except Exception as e:           # never let this break the page
            print("Outage scan: %s" % e)
            outages = []
        rate = hours_per_day(state.get("history", []), outages)
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
         order_button + ordered_form(state, level),
         "%.0f h" % left if left > 0 else "now",
         " (last known)" if error else " now", format(now_hours, ",.0f"),
         delivered_on, format(baseline, ",.0f"),
         season, format(baseline + order, ",.0f"), order,
         format(baseline + stop, ",.0f"), stop, eta,
         cycling_rows(state.get("cycles", []), now_hours, outages)
         + weather_rows(extra, state.get("cycles", []), outages)))
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

def run(delivered=False, new_baseline=None, interactive=False, attempts=3):
    """One check. With delivered=True, first restart the count from
    new_baseline (or from the boiler's current reading when None).
    interactive=True: started from the page or by hand, so no scheduler email
    backs up a failed alert email; such an alert is kept as pending and the
    next scheduled check repeats it."""
    with Lock():
        state = load_state()             # SetupError if damaged
        first_run = state is None
        if first_run:
            state = {"baseline": FIRST_BASELINE, "level": "ok", "history": []}

        try:
            now_hours, starts, extra = read_boiler(attempts=attempts)
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
                 "old_last_reminder": state.get("last_reminder"),
                 "old_ordered": state.get("ordered")})
            state["deliveries"] = state["deliveries"][-50:]
            state["baseline"] = new
            state["level"] = "ok"
            state["history"] = []
            state["notified"] = []
            state.pop("last_reminder", None)
            state.pop("ordered", None)
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
        if not interactive:
            pending = state.pop("pending_alert", None)
            if pending and not alert:    # an earlier button press couldn't send it
                alert = pending
        hint = setpoint_hint(state, extra)
        state["level"] = level
        save_state(state)
        page_ok = write_page(render_page(now_hours, state, level, extra=extra))

    rc = 0 if page_ok else 2
    if alert:
        url = ORDER_URL if level in ("order", "stop") else ""
        subject = alert.split(". ")[0].rstrip(".")      # first sentence
        # Headline first: it is the first line of the DSM Task Scheduler email.
        print(alert)
        if url:
            print("Order pellets here: %s" % url)
        print(summary)
        mailed = send_mail(subject, alert_text(alert, summary, url),
                           alert_html(alert, summary, url))
        push(subject, alert_text(alert, summary, ""), url)
        if mailed:
            print("Alert emailed to %s." % ", ".join(MAIL_TO or [SMTP_USER]))
            rc = 0                       # our own email went out: no DSM email
        else:
            rc = 1                       # no email of ours: let DSM send it
            if interactive:              # ...but no DSM here: repeat it later
                _update_state(lambda st: st.__setitem__("pending_alert", alert))
                print("The alert email could not be sent; the next scheduled "
                      "check will repeat it.")
    else:
        print(summary)
    if first_run and not delivered:
        print("First run: counting from %.0f h. If your last delivery was "
              "earlier, run with --delivered <hours at delivery>." % state["baseline"])
        rc = 1
    if delivered:
        push("Pellet count restarted", summary)
        send_mail("Pellet count restarted", summary)
    try:                                 # backup for the minute logger
        if not boiler_error_step(extra, from_check=True):
            rc = 1                       # alert not delivered: let DSM email
    except Exception as e:
        print("boiler error check: %s" % e)
    if hint:                             # after the alert: alert stays first
        title, msg = hint
        print(title + ". " + msg)
        pushed = push(title, msg, PAGE_URL, priority="high")
        mailed = send_mail(title, msg)   # both, so it isn't missed
        sent = pushed or mailed
        if not sent:                     # undo the record so it is tried again
            _update_state(lambda st: st.pop("setpoint_hint", None))
    return rc


def _update_state(change):
    """Apply a small change to the saved state under the lock."""
    try:
        with Lock():
            st = load_state()
            if st is not None and change(st) is not False:
                save_state(st)
    except Exception as e:
        print("Could not update the state file: %s" % e)


def test_notify():
    """Send a test email and push with the current settings."""
    if not SMTP_USER and not NTFY_TOPIC:
        print("Neither SMTP_USER nor NTFY_TOPIC is set; nothing to test.")
        return 2
    headline = ("This is a test from pellet_watch. If you can read it, "
                "alerts will reach you.")
    ok = True
    mailed = send_mail("Pellet watch test",
                       alert_text(headline, "No action needed.", ORDER_URL),
                       alert_html(headline, "No action needed.", ORDER_URL))
    if mailed is not None:
        print("Email to %s: %s" % (", ".join(MAIL_TO or [SMTP_USER]),
                                   "sent" if mailed else "FAILED"))
        ok = ok and mailed
    pushed = push("Pellet watch test", "Test push from pellet_watch.", ORDER_URL,
                  priority="default")
    if pushed is not None:
        print("ntfy push to topic: %s" % ("sent" if pushed else "FAILED"))
        ok = ok and pushed
    return 0 if ok else 2


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
    sug, note = advised_setpoint(extra.get("outdoor"))
    print("Outdoor (online weather): %s, forecast for the coming day %s. Boiler water %s, setpoint %s, state %s "
          "(%s), modulation %s. Suggested setpoint (advisory): %s%s."
          % (fmt_temp(extra.get("outdoor"), 1), fmt_temp(extra.get("forecast"), 1),
             fmt_temp(extra.get("boiler"), 1),
             fmt_temp(_setpoint(extra), 1) if _setpoint(extra) is not None
             else "none (not heating)", extra.get("statetext") or "?",
             extra.get("state") if extra.get("state") is not None else "?",
             "?" if extra.get("modulation") is None else "%.0f%%" % extra["modulation"],
             fmt_temp(sug, 1), note))
    return 0


def set_ordered(on):
    """Record (on=True) or cancel (on=False) that pellets have been ordered,
    then run a normal check so the page shows it."""
    with Lock():
        state = load_state()
        if state is None:
            print("No count yet: run pellet_watch.py once first.")
            return 2
        if on:
            if state.get("level", "ok") == "ok":
                print("Not past the order point yet; nothing to pause.")
                return 2
            state["ordered"] = time.time()
            msg = ("Marked as ordered. Daily reminders are paused until the "
                   "delivery is recorded (or for %g days). The hard-stop alert "
                   "is still sent." % ORDERED_QUIET_DAYS)
        else:
            if not state.pop("ordered", None):
                print("Pellets were not marked as ordered.")
                return 2
            msg = "Order mark removed: daily reminders resume."
        save_state(state)
    print(msg)
    sys.stdout.flush()
    try:                                 # refresh the page; one quick try
        rc = run(interactive=True, attempts=1)
    except Exception as e:
        print("The mark is saved; the page updates at the next check (%s)." % e)
        return 0
    return 0                             # the mark itself was saved


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
        for key in ("notified", "last_reminder", "ordered"):
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
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):         # [IPv6]:port
            return host[1:].split("]", 1)[0]
        return host.rsplit(":", 1)[0] if host.count(":") == 1 else host

    def _host_ok(self):
        """Only names this machine is really reached by: an IP address,
        localhost, its own hostname, or the PAGE_URL host. Blocks DNS
        rebinding (a hostile site's name pointed at this machine)."""
        host = self._host()
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        name = socket.gethostname().lower()
        allowed = {"localhost", name, name.split(".")[0]}
        if PAGE_URL:
            allowed.add((urllib.parse.urlsplit(PAGE_URL).hostname or "").lower())
        allowed.update(h.lower() for h in ALLOWED_HOSTS)
        return host in allowed or host.split(".")[0] in allowed

    def do_GET(self):
        # Also serves the status page itself, so no other web server is needed
        # (e.g. on a Raspberry Pi): http://<this machine>:SERVE_PORT/
        if not self._host_ok():
            return self._send(403, "<p>Forbidden.</p>")
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
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/reset", "/ordered"):
            return self._send(404, "Not found")
        # Only accept the form from a page on this same machine.
        if not self._host_ok():
            return self._send(403, "<p>Forbidden.</p>")
        host = self._host()
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if value is None:
                continue
            if value == "null" or (urllib.parse.urlsplit(value).hostname
                                   or "").lower() != host:
                return self._send(403, "<p>Forbidden.</p>")
            break
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        length = max(0, min(length, 1000))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        back_link = "<a href='javascript:history.back()'>Back</a>"
        if path == "/ordered":
            action = form.get("action", [""])[0]
            if action not in ("yes", "cancel"):
                return self._send(400, "<p>Unknown action. %s</p>" % back_link)
            command = ["--ordered", action]
        else:
            try:
                hours = float(form.get("hours", [""])[0].replace(",", ""))
                if not math.isfinite(hours) or hours <= 0:
                    raise ValueError
            except ValueError:
                return self._send(400, "<p>Please enter the burner hours as a "
                                  "number. %s</p>" % back_link)
            command = ["--delivered", "%.0f" % hours]
        # Run the current version of the script (not the copy this server
        # loaded at startup), so updates take effect without a restart.
        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT_PATH] + command,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=150,
                cwd=HERE)
            output = proc.stdout.decode("utf-8", "replace").strip()
            ok = proc.returncode in (0, 1)
        except subprocess.TimeoutExpired:
            output, ok = "Timed out waiting for the boiler.", False
        print(output)
        if not ok:
            what = ("The order mark was not changed." if path == "/ordered"
                    else "The count was not restarted.")
            return self._send(400, "<p>%s</p><pre>%s</pre><p>%s</p>"
                              % (what, html.escape(output), back_link))

        # Back to the page; never to an address taken from the request.
        self._send(303, "<p>Done.</p>", location=PAGE_URL or "/pellets.html")

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))
        sys.stdout.flush()


ECOBEE_FIELDS = ["time_local", "time_utc", "thermostat_id", "thermostat",
                 "heat_seconds", "fan_seconds", "indoor_f", "setpoint_heat_f",
                 "outdoor_f", "system_mode", "climate", "event"]


def beestat_call(resource, method, args=None):
    q = {"api_key": BEESTAT_API_KEY, "resource": resource, "method": method}
    if args is not None:
        q["arguments"] = json.dumps(args)
    url = BEESTAT_URL + "?" + urllib.parse.urlencode(q)
    reply = json.loads(urllib.request.urlopen(url, timeout=60).read().decode("utf-8"))
    if not reply.get("success"):
        data = reply.get("data")
        raise RuntimeError("beestat %s.%s failed: %s" % (
            resource, method, data.get("error_message") if isinstance(data, dict)
            else data))
    return reply.get("data")


def prune_logs(pattern):
    logs = sorted(glob.glob(os.path.join(DATA_PATH, pattern)))
    for old in logs[:-max(1, LOG_KEEP_MONTHS)]:
        try:
            os.remove(old)
        except OSError:
            pass


def fetch_ecobee():
    """Save new ecobee runtime records (5-minute rows) from beestat to
    pellet_watch_ecobee_YYYY-MM.csv. Returns a short summary string."""
    try:
        with open(BEESTAT_STATE_PATH, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    thermostats = beestat_call("thermostat", "read_id")
    if isinstance(thermostats, dict):
        thermostats = list(thermostats.values())
    try:                                 # beestat syncs slowly when idle
        beestat_call("thermostat", "sync")
    except Exception:
        pass
    added, failed = [], []
    try:
        for th in thermostats:
            tid = th.get("thermostat_id")
            if tid is None:
                continue
            try:
                added.append(_fetch_one(th, tid, seen))
            except Exception as e:
                failed.append("%s (%s)" % (th.get("name") or tid,
                                           str(e).replace(BEESTAT_API_KEY, "***")))
    finally:                             # save progress even if one failed
        tmp = "%s.%d.tmp" % (BEESTAT_STATE_PATH, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2)
        os.replace(tmp, BEESTAT_STATE_PATH)
    prune_logs("pellet_watch_ecobee_????-??.csv")
    if failed:
        raise RuntimeError("saved %s; failed: %s" % (", ".join(added) or "nothing",
                                                     ", ".join(failed)))
    return "ecobee records saved: " + ", ".join(added)


def _fetch_one(th, tid, seen):
    """New records for one thermostat; updates seen. Returns 'name n'."""
    try:
        beestat_call("runtime", "sync", {"thermostat_id": tid})
    except Exception:
        pass
    since = seen.get(str(tid)) or time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 7 * 86400))
    oldest = time.strftime("%Y-%m-%d %H:%M:%S",   # beestat max range: 31 days
                           time.gmtime(time.time() - 30 * 86400))
    since = max(since, oldest)
    rows = beestat_call("runtime_thermostat", "read", {"attributes": {
        "thermostat_id": tid, "timestamp": {"value": since, "operator": ">"}}})
    if isinstance(rows, dict):
        rows = list(rows.values())
    rows = sorted(rows or [], key=lambda r: r.get("timestamp") or "")
    n = 0
    for r in rows:
        row = _ecobee_row(r, tid, th.get("name"))
        if row is None:
            continue
        local, utc = row[0], row[1]
        path = os.path.join(DATA_PATH, "pellet_watch_ecobee_%s.csv" % local[:7])
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(ECOBEE_FIELDS)
            w.writerow(["" if v is None else v for v in row])
        seen[str(tid)] = utc
        n += 1
    return "%s %d" % (th.get("name") or tid, n)


def _beestat_failures(reset=False):
    """Count consecutive beestat failures in the state file; returns the count."""
    n = [0]

    def change(st):
        if reset:
            return False if st.pop("beestat_failures", None) is None else None
        n[0] = st.get("beestat_failures", 0) + 1
        st["beestat_failures"] = n[0]
    _update_state(change)
    return n[0]


def _ecobee_row(r, tid, name):
    """One beestat runtime record as a CSV row (ECOBEE_FIELDS), or None."""
    ts = r.get("timestamp")
    if not ts:
        return None
    utc = ts[:19].replace("T", " ")      # "2026-11-17T13:10:00+00:00"
    try:
        local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_utc_epoch(utc)))
    except ValueError:
        return None
    heat = (r.get("auxiliary_heat_1") or 0) + (r.get("auxiliary_heat_2") or 0)
    return [local, utc, tid, name, heat, r.get("fan"),
            r.get("indoor_temperature"), r.get("setpoint_heat"),
            r.get("outdoor_temperature"), r.get("system_mode"),
            r.get("climate"), r.get("event")]


def ecobee_history(first_day, last_day):
    """One-off download of past ecobee records (beestat keeps about 13 months)
    into pellet_watch_ecobee_history_YYYY-MM.csv, one file per month, both
    thermostats together. Files are rewritten, so re-running is safe; they
    are never pruned. Does not touch the regular ecobee files."""
    if not BEESTAT_API_KEY:
        raise SetupError("BEESTAT_API_KEY is not set in the config file.")
    try:
        start = time.mktime(time.strptime(first_day, "%Y-%m-%d"))
        end = time.mktime(time.strptime(last_day, "%Y-%m-%d")) + 86400
    except ValueError:
        raise SetupError("Dates must look like 2025-11-01.")
    if end <= start:
        raise SetupError("The second date must not be before the first.")
    thermostats = beestat_call("thermostat", "read_id")
    if isinstance(thermostats, dict):
        thermostats = list(thermostats.values())
    months, seen, counts, failure = {}, set(), {}, None
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    for th in thermostats:
        tid = th.get("thermostat_id")
        if tid is None:
            continue
        name = th.get("name") or str(tid)
        counts[name] = 0
        chunk = start
        while chunk < end:               # a week per request keeps replies small
            stop = min(chunk + 7 * 86400, end)
            args = {"attributes": {
                "thermostat_id": tid,
                "timestamp": {"value": [fmt(chunk), fmt(stop)],
                              "operator": "between"}}}
            for attempt in range(4):     # beestat rate-limits bursts
                try:
                    rows = beestat_call("runtime_thermostat", "read", args)
                    break
                except Exception as e:
                    if attempt == 3:
                        failure = str(e).replace(BEESTAT_API_KEY, "***")
                        rows = None
                        break
                    time.sleep(15 * (attempt + 1))
            if rows is None:
                break
            if isinstance(rows, dict):
                rows = list(rows.values())
            for r in rows or []:
                row = _ecobee_row(r, tid, name)
                if row is None or (tid, row[1]) in seen:
                    continue
                if not (start <= _utc_epoch(row[1]) < end):
                    continue
                seen.add((tid, row[1]))
                months.setdefault(row[0][:7], []).append(row)
                counts[name] += 1
            print("  %s: %s to %s done" % (name, time.strftime("%Y-%m-%d", time.localtime(chunk)),
                                          time.strftime("%Y-%m-%d", time.localtime(stop - 1))))
            sys.stdout.flush()
            chunk = stop
        if failure:
            break
    for month, rows in sorted(months.items()):
        rows.sort(key=lambda r: (r[0], str(r[2])))
        path = os.path.join(DATA_PATH, "pellet_watch_ecobee_history_%s.csv" % month)
        if failure and os.path.exists(path):
            print("Kept the existing %s (this run is incomplete)"
                  % os.path.basename(path))
            continue
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(ECOBEE_FIELDS)
            for row in rows:
                w.writerow(["" if v is None else v for v in row])
        os.replace(tmp, path)
        print("Wrote %s (%d rows)" % (os.path.basename(path), len(rows)))
    if failure:
        print("beestat stopped answering (%s). Months above may be partial: run "
              "the same command again later; complete files replace them."
              % failure)
        return 2
    if not months:
        print("No ecobee records found for %s to %s (beestat keeps about "
              "13 months)." % (first_day, last_day))
        return 2
    print("ecobee history saved: " + ", ".join("%s %d" % kv for kv in counts.items()))
    return 0


def _utc_epoch(utc_text):
    return calendar.timegm(time.strptime(utc_text, "%Y-%m-%d %H:%M:%S"))


# --- ecobee control (heat-dump trial) ---------------------------------------

ECOBEE_AUTH_URL = "https://auth.ecobee.com/oauth/token"
ECOBEE_API_URL = "https://api.ecobee.com/1/thermostat"
ECOBEE_CLIENT_ID = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"   # ecobee web client
ECOBEE_TOKEN_LOCK = os.path.join(HERE, "pellet_watch_ecobee.lock")
_ecobee_access = {"token": None, "expires": 0}
ECOBEE_LOCK = threading.Lock()


class _TokenLock:
    """Only one process refreshes the ecobee token at a time: a refresh token
    may be single-use, so two refreshes with the same one can revoke it."""
    def __enter__(self):
        self.f = open(ECOBEE_TOKEN_LOCK, "a")
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _ecobee_access_token(force=False):
    """Access token from the saved refresh token (rotated tokens are saved)."""
    if not force and _ecobee_access["token"] and time.time() < _ecobee_access["expires"]:
        return _ecobee_access["token"]
    with _TokenLock():                   # re-read inside: another process may
        try:                             # have just rotated it
            with open(ECOBEE_TOKEN_PATH, encoding="utf-8") as f:
                refresh = json.load(f)["refresh_token"]
        except (OSError, ValueError, KeyError):
            raise RuntimeError("no ecobee sign-in: run tools/ecobee_login.py and upload "
                               "pellet_watch_ecobee_token.json")
        data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh,
                                       "client_id": ECOBEE_CLIENT_ID}).encode()
        try:
            reply = json.loads(urllib.request.urlopen(
                urllib.request.Request(ECOBEE_AUTH_URL, data=data), timeout=30).read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise RuntimeError("ecobee sign-in expired: run tools/ecobee_login.py again")
            raise RuntimeError("ecobee sign-in failed (HTTP %d)" % e.code)
        token = reply.get("access_token")
        if not token:
            raise RuntimeError("ecobee sign-in returned no access token")
        if reply.get("refresh_token") and reply["refresh_token"] != refresh:
            tmp = "%s.%d.tmp" % (ECOBEE_TOKEN_PATH, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"refresh_token": reply["refresh_token"]}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ECOBEE_TOKEN_PATH)
    _ecobee_access.update(token=token,
                          expires=time.time() + int(reply.get("expires_in", 3600)) - 120)
    return token


def ecobee_request(body=None, query=None):
    """GET (query) or POST (body) to the ecobee thermostat API."""
    for attempt in (0, 1):
        token = _ecobee_access_token(force=attempt == 1)
        if body is None:
            req = urllib.request.Request(
                ECOBEE_API_URL + "?" + urllib.parse.urlencode({"json": json.dumps(query)}))
        else:
            req = urllib.request.Request(ECOBEE_API_URL, data=json.dumps(body).encode())
            req.add_header("Content-Type", "application/json;charset=UTF-8")
        req.add_header("Authorization", "Bearer " + token)
        try:
            reply = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except urllib.error.HTTPError as e:
            try:
                status = json.loads(e.read()).get("status", {})
            except Exception:
                status = {}
            # ecobee reports an expired token as 500 + status 14 (or 16)
            if attempt == 0 and (e.code in (401, 403) or status.get("code") in (14, 16)):
                continue
            raise RuntimeError("ecobee API error %d: %s" % (e.code, status.get("message") or e.reason))
        status = reply.get("status", {})
        if status.get("code", 0) != 0:
            if status.get("code") in (14, 16) and attempt == 0:
                continue
            raise RuntimeError("ecobee API: %s" % status.get("message"))
        return reply
    raise RuntimeError("ecobee API: not authorised")


def ecobee_thermostats(identifier=None):
    """[{identifier, name, time, heat, cool, actual, mode, calling, events}].
    Temperatures in tenths of degF, as ecobee gives them."""
    sel = ({"selectionType": "thermostats", "selectionMatch": identifier} if identifier
           else {"selectionType": "registered", "selectionMatch": ""})
    sel.update(includeRuntime=True, includeSettings=True, includeEquipmentStatus=True,
               includeEvents=True)
    reply = ecobee_request(query={"selection": sel})
    out = []
    for t in reply.get("thermostatList", []):
        status = t.get("equipmentStatus") or ""
        rt = t.get("runtime", {})
        out.append({"identifier": t.get("identifier"), "name": t.get("name"),
                    "time": t.get("thermostatTime"),
                    "heat": rt.get("desiredHeat"), "cool": rt.get("desiredCool"),
                    "actual": rt.get("actualTemperature"),
                    "mode": t.get("settings", {}).get("hvacMode"),
                    "calling": "auxHeat" in status or "heatPump" in status,
                    "events": t.get("events") or []})
    return out


def _dump_target(stats):
    for t in stats:
        if (t["name"] or "").lower() == ECOBEE_DUMP_THERMOSTAT.lower():
            return t
    raise RuntimeError("no ecobee thermostat named %r (found: %s)" % (
        ECOBEE_DUMP_THERMOSTAT, ", ".join(t["name"] or "?" for t in stats)))


def ecobee_hold(t, minutes, bump_f):
    """Hold t's heat setpoint bump_f above the room (or the setpoint, if
    higher) for minutes. The thermostat ends the hold itself, so a crash
    here can't leave it on. Returns (heat, end) to recognise it later."""
    if not t.get("time"):
        raise RuntimeError("thermostat gave no local time; not setting a hold")
    base = calendar.timegm(time.strptime(t["time"], "%Y-%m-%d %H:%M:%S"))
    heat = max(int(t["heat"]), int(t.get("actual") or 0)) + int(round(bump_f * 10))
    cool = max(int(t["cool"] or 0), heat + 50)
    start = time.gmtime(base - 60)
    end = time.gmtime(base + minutes * 60)
    end_s = (time.strftime("%Y-%m-%d", end), time.strftime("%H:%M:%S", end))
    try:
        _set_hold(t, heat, cool, start, end_s)
    except Exception as e:
        e.hold = (heat, end_s)           # it may have been applied anyway
        raise
    return heat, end_s


def _set_hold(t, heat, cool, start, end_s):
    ecobee_request(body={
        "selection": {"selectionType": "thermostats", "selectionMatch": t["identifier"]},
        "functions": [{"type": "setHold", "params": {
            "holdType": "dateTime", "heatHoldTemp": heat, "coolHoldTemp": cool,
            "startDate": time.strftime("%Y-%m-%d", start),
            "startTime": time.strftime("%H:%M:%S", start),
            "endDate": end_s[0], "endTime": end_s[1]}}]})


def _our_hold_on_top(t, heat, end_s):
    """True if the running hold on top is the dump's own one."""
    for ev in t["events"]:
        if ev.get("running"):
            return (ev.get("type") == "hold" and ev.get("heatHoldTemp") == heat
                    and (ev.get("endDate"), ev.get("endTime")) == tuple(end_s))
    return False


def ecobee_release(identifier, heat, end_s):
    """Resume the schedule only if the dump's hold is still the one on top,
    so a change someone made at the thermostat is never undone.
    Returns a short note for the log."""
    t = ecobee_thermostats(identifier)[0]
    if not _our_hold_on_top(t, heat, end_s):
        return "hold already gone or replaced; left alone"
    ecobee_request(body={
        "selection": {"selectionType": "thermostats", "selectionMatch": identifier},
        "functions": [{"type": "resumeProgram", "params": {"resumeAll": False}}]})
    return "released"


DUMP_FIELDS = ["time", "event", "boiler_c", "thermostat", "detail"]
_dump = {"active": None, "cooldown": 0, "day": None, "count": 0, "fail_noted": False,
         "last": []}
FIRING_STATES = (1, 2, 3, 4)             # start, ignition, softstart, powerburn
DUMP_CHECK_S = 8 * 60                    # by now the water must be cooling


def _dump_log(event, boiler, name="", detail=""):
    try:
        path = os.path.join(DATA_PATH, time.strftime("pellet_watch_dump_%Y-%m.csv"))
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(DUMP_FIELDS)
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), event,
                        "" if boiler is None else boiler, name, detail])
        prune_logs("pellet_watch_dump_????-??.csv")
    except OSError as e:
        print("dump log: %s" % e)


def _end_dump(boiler, why, release=True):
    a, _dump["active"] = _dump["active"], None     # cleared first: never repeats
    _dump["cooldown"] = a["now"] + 10 * 60
    if release and not a.get("dry"):
        try:
            why += "; " + ecobee_release(a["id"], a["heat"], a["end"])
        except Exception as e:
            why += "; release failed: %s (the hold ends itself)" % e
    _dump_log("end", boiler, a["name"], "%s after %.0f min, peak %s" % (
        why, (a["now"] - a["start"]) / 60, a["peak"]))
    if ECOBEE_DUMP_NOTIFY and not a.get("dry"):
        _notify_dump(a, boiler, why)


def _notify_dump(a, boiler, why):
    """One quiet push per dump, when it ends."""
    hm = lambda t: time.strftime("%H:%M", time.localtime(t))
    mins = max(1, round((a["now"] - a["start"]) / 60))
    early = why.startswith(("water not cooling", "no boiler reading", "burner fired"))
    title = ("ecobee dump ended early: %s" % a["name"] if early
             else "ecobee dump: %s, %d min" % (a["name"], mins))
    msg = ("%s-%s: boiler water %s, peak %s, now %s. Ended: %s."
           % (hm(a["start"]), hm(a["now"]), fmt_temp(a.get("b0", a["peak"])),
              fmt_temp(a["peak"]), "?" if boiler is None else fmt_temp(boiler), why))
    try:
        push(title, msg, PAGE_URL, priority="low")
    except Exception as e:               # never let this break the logger
        print("dump push: %s" % e)


def heat_dump_step(extra, now=None):
    """Called once a minute by the logger with fresh boiler readings."""
    now = now or time.time()
    boiler, state = extra.get("boiler"), extra.get("state")
    last = _dump["last"]
    last.append(boiler)
    del last[:-3]
    with ECOBEE_LOCK:
        a = _dump["active"]
        if a:
            a["now"] = now
            elapsed = now - a["start"]
            if not ECOBEE_DUMP:
                return _end_dump(boiler, "dump turned off")
            if boiler is None:
                return _end_dump(boiler, "no boiler reading")
            a["peak"] = max(a["peak"], boiler)
            if state in FIRING_STATES:
                return _end_dump(boiler, "burner fired during the dump")
            if boiler <= ECOBEE_DUMP_STOP_C:
                return _end_dump(boiler, "boiler down to %s" % fmt_temp(boiler))
            if elapsed >= ECOBEE_DUMP_MINUTES * 60:
                return _end_dump(boiler, "time limit")
            # ecobee's reported status lags by minutes, so judge by the water:
            # once the zone's valve is open it cools within a few minutes.
            if (not a["checked"] and not a.get("dry") and elapsed >= DUMP_CHECK_S
                    and boiler > a["peak"] - 1.0):
                return _end_dump(boiler, "water not cooling: the zone did not open")
            return
        if not ECOBEE_DUMP or boiler is None:
            return
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if _dump["day"] != today:
            _dump.update(day=today, count=0)
        if (boiler < ECOBEE_DUMP_START_C or state in FIRING_STATES
                or state == ERROR_STATE
                or now < _dump["cooldown"] or _dump["count"] >= ECOBEE_DUMP_MAX_PER_DAY):
            return
        prev = [b for b in last[:-1] if b is not None]
        if prev and boiler < max(prev):  # already falling: a zone (likely the
            return                       # tank) is taking the heat
        try:
            stats = ecobee_thermostats()
            t = _dump_target(stats)
            _dump["fail_noted"] = False
        except Exception as e:
            _dump["cooldown"] = now + 15 * 60
            _dump_log("error", boiler, "", str(e))
            if not _dump["fail_noted"]:
                _dump["fail_noted"] = True
                push("ecobee heat dump: cannot reach the thermostat", str(e),
                     priority="default")
            return
        if t["mode"] not in ("heat", "auto", "auxHeatOnly"):
            _dump["cooldown"] = now + 60 * 60
            _dump_log("skip", boiler, t["name"], "thermostat mode is %s" % t["mode"])
            return
        busy = [x["name"] for x in stats if x["calling"]]
        if busy:                         # a zone is open: the pump is moving the heat
            _dump["cooldown"] = now + 5 * 60
            _dump_log("skip", boiler, t["name"], "already calling: %s" % ", ".join(busy))
            return
        _dump["count"] += 1
        if ECOBEE_DUMP_DRY_RUN:
            _dump["active"] = {"id": t["identifier"], "name": t["name"], "heat": None,
                               "end": None, "start": now, "now": now, "peak": boiler,
                               "checked": True, "dry": True}
            _dump_log("would start", boiler, t["name"], "dry run")
            return
        try:
            heat, end_s = ecobee_hold(t, ECOBEE_DUMP_MINUTES, ECOBEE_DUMP_BUMP_F)
        except Exception as e:
            _dump["cooldown"] = now + 15 * 60
            note = "hold failed: %s" % e
            if getattr(e, "hold", None):  # e.g. a timeout after ecobee applied it
                try:
                    note += "; " + ecobee_release(t["identifier"], *e.hold)
                except Exception as e2:
                    note += "; release check failed: %s (the hold ends itself)" % e2
            _dump_log("error", boiler, t["name"], note)
            return
        _dump["active"] = {"id": t["identifier"], "name": t["name"], "heat": heat,
                           "end": end_s, "start": now, "now": now, "peak": boiler,
                           "b0": boiler, "checked": False}
        _dump_log("start", boiler, t["name"], "hold %.1f degF until %s (room %.1f)"
                  % (heat / 10.0, end_s[1][:5], (t.get("actual") or 0) / 10.0))


def ecobee_status():
    """--ecobee-status: show what the script sees (read-only)."""
    try:
        stats = ecobee_thermostats()
        for t in stats:
            print("%-14s room %.1f degF, heat set %.1f degF, mode %s, %s" % (
                t["name"], (t["actual"] or 0) / 10.0, (t["heat"] or 0) / 10.0, t["mode"],
                "calling for heat" if t["calling"] else "idle"))
        print("ecobee sign-in works.")
        if ECOBEE_DUMP_THERMOSTAT:
            print("Dump thermostat: %s" % _dump_target(stats)["name"])
        else:
            print("Set ECOBEE_DUMP_THERMOSTAT to one of the names above.")
        return 0
    except Exception as e:
        print("ecobee: %s" % e)
        return 2


def ecobee_dump_test(minutes=3):
    """--ecobee-dump-test: a real short hold on the dump thermostat."""
    try:
        t = _dump_target(ecobee_thermostats())
        if t["mode"] not in ("heat", "auto", "auxHeatOnly"):
            print("%s is in %s mode; not testing." % (t["name"], t["mode"]))
            return 2
        heat, end_s = ecobee_hold(t, minutes, ECOBEE_DUMP_BUMP_F)
    except Exception as e:
        print("ecobee: %s" % e)
        return 2
    print("Hold set on %s: %.1f degF until %s; it ends itself. Its zone should "
          "start calling within a minute or two." % (t["name"], heat / 10.0, end_s[1][:5]))
    return 0


# --- boiler error alert ------------------------------------------------------

ERROR_STATE = 11
ERROR_CLEAR_MINUTES = 5                  # running this long before "running again"


def _logger_running():
    """True if the minute logger wrote a row in the last few minutes."""
    try:
        newest = max(glob.glob(os.path.join(DATA_PATH, "pellet_watch_log_????-??.csv")),
                     key=os.path.getmtime)
        return time.time() - os.path.getmtime(newest) < max(300, 3 * LOG_SECONDS)
    except (ValueError, OSError):
        return False


def boiler_error_step(extra, now=None, from_check=False):
    """Alert once when the boiler sits in its error state; note recovery.
    Returns False if an error alert was due but could not be sent."""
    if not ERROR_ALERT or extra.get("state") is None:
        return True
    now = now or time.time()
    wait = ERROR_ALERT_MINUTES * 60
    if from_check and not _logger_running():
        wait = 0                         # no minute logger: alert at first sight
    news = []

    def change(st):
        err = st.get("boiler_error")
        if extra.get("state") == ERROR_STATE:
            if not err:
                err = st["boiler_error"] = {"since": now, "alerted": False}
            changed = err.pop("ok_since", None) is not None
            if not err["alerted"] and now - err["since"] >= wait:
                err["alerted"] = True    # claimed under the lock: sent once
                news.append(("error", err["since"]))
                return None
            return None if changed or err["since"] == now else False
        if not err:
            return False
        if not err["alerted"]:           # a blip that never alerted
            st.pop("boiler_error")
            return None
        if "ok_since" not in err:
            err["ok_since"] = now
            return None
        if now - err["ok_since"] >= ERROR_CLEAR_MINUTES * 60:
            st.pop("boiler_error")
            news.append(("ok", err["since"]))
            return None
        return False
    _update_state(change)
    delivered = True
    for kind, since in news:
        when = time.strftime("%H:%M", time.localtime(since))
        if kind == "error":
            title = "Boiler stopped with an error"
            msg = ("The boiler has reported an error since %s and is not heating "
                   "(water %s). Check myPelletronic > General > Info for the code. "
                   "A [5043] vacuum system error usually means pellets are not "
                   "reaching the pickup." % (when, fmt_temp(extra.get("boiler"))))
        else:
            title = "Boiler running again"
            msg = "The boiler error from %s has cleared." % when
        sent = send_mail(title, msg)
        pushed = push(title, msg, PAGE_URL, priority="high" if kind == "error" else "default")
        print("%s. %s" % (title, msg))
        if kind == "error" and not sent and not pushed:
            delivered = False            # un-claim it so the next minute retries

            def unclaim(st):
                if st.get("boiler_error"):
                    st["boiler_error"]["alerted"] = False
                    return None
                return False
            _update_state(unclaim)
            print("(No email or push could be sent; will try again.)")
    return delivered


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
    path = os.path.join(DATA_PATH, time.strftime("pellet_watch_log_%Y-%m.csv"))
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_FIELDS)
        w.writerow(["" if v is None else v for v in row])
    prune_logs("pellet_watch_log_????-??.csv")
    for step in (boiler_error_step, heat_dump_step):
        try:
            step(extra)
        except Exception as e:           # never let these stop the logger
            print("%s: %s" % (step.__name__, e))


def _config_mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


def logger_loop():
    seen = _config_mtime()
    while True:
        start = time.time()
        now = _config_mtime()
        if now != seen:                  # settings changed: pick them up
            seen = now
            try:
                load_config()
            except SetupError as e:
                print("Settings file has a problem, keeping the old settings: %s" % e)
        try:
            log_once()
        except Exception as e:           # never let logging stop the server
            print("Log error: %s" % e)
        time.sleep(max(1.0, LOG_SECONDS - (time.time() - start)))


def serve():
    if LOG_SECONDS:
        threading.Thread(target=logger_loop, daemon=True).start()
        print("Logging boiler readings every %d s to %s" % (
            LOG_SECONDS, os.path.join(DATA_PATH, "pellet_watch_log_YYYY-MM.csv")))
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
    g.add_argument("--ordered", nargs="?", const="yes", choices=["yes", "cancel"],
                   help="pellets ordered: pause the daily reminders until the "
                        "delivery (--ordered cancel to resume them)")
    g.add_argument("--serve", action="store_true",
                   help="serve the status page and its reset button")
    g.add_argument("--ecobee-history", nargs=2, metavar=("FROM", "TO"),
                   help="one-off: save past ecobee records from beestat, e.g. "
                        "2025-11-01 2026-04-30")
    g.add_argument("--ecobee-status", action="store_true",
                   help="show the ecobee thermostats the heat dump would use")
    g.add_argument("--ecobee-dump-test", action="store_true",
                   help="set a real 3-minute dump hold to test the ecobee control")
    g.add_argument("--test-notify", action="store_true",
                   help="send a test email / ntfy push with the current settings")
    args = p.parse_args(argv)
    try:
        load_config()
        if args.serve:
            serve()
            return 0
        if args.test_notify:
            return test_notify()
        if args.ecobee_status:
            return ecobee_status()
        if args.ecobee_dump_test:
            return ecobee_dump_test()
        if args.ecobee_history:
            return ecobee_history(*args.ecobee_history)
        if args.status:
            return status()
        if args.undo:
            return undo()
        if args.ordered:
            return set_ordered(args.ordered == "yes")
        if args.delivered is not None:
            if args.delivered == "now":
                return run(True, None, interactive=True)
            try:
                value = float(args.delivered.replace(",", ""))
            except ValueError:
                raise SetupError("%r is not a number." % args.delivered)
            return run(True, value, interactive=True)
        rc = run()
        if BEESTAT_API_KEY and rc != 2:
            # After the alert lines, so the alert stays first in the email.
            try:
                print(fetch_ecobee())
                _beestat_failures(reset=True)
            except Exception as e:       # never let beestat affect the check
                text = str(e).replace(BEESTAT_API_KEY, "***")
                print("beestat: %s" % text)
                if _beestat_failures() == 3:     # tell once, not every check
                    msg = ("ecobee records have failed 3 checks in a row (%s). "
                           "Check the beestat API key and beestat.io." % text)
                    if not push("ecobee logging is failing", msg, priority="default"):
                        send_mail("ecobee logging is failing", msg)
        return rc
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
