#!/usr/bin/env python3
"""
pellet_watch.py - Okofen pellet reorder reminder for a Synology NAS.

Reads "PE1 Burner Run Time" from the boiler's local JSON interface, compares it
with the reading at the last pellet delivery, writes a small status web page,
and raises an alert at the order point (650 h in winter, 675 h in summer) and
again at the hard stop (700 h).

Usage:
    python3 pellet_watch.py               check, update the web page, alert if due
    python3 pellet_watch.py --delivered   pellets just arrived: restart the count
                                          from the boiler's current reading
    python3 pellet_watch.py --delivered 12543
                                          restart the count from a given reading
    python3 pellet_watch.py --serve       keep running: serves the status page and
                                          handles its "Pellets delivered" button

Exit code: 0 = fine, 1 = alert on this check (heads-up, order point, hard stop or
daily reminder), 2 = could not read boiler.
(DSM Task Scheduler can email you whenever the exit code is not 0.)
"""

import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# ----------------------------- SETTINGS ------------------------------------
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
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "pellets-icon.png")
STATE_PATH = os.path.join(HERE, "pellet_watch_state.json")
CONFIG_PATH = os.path.join(HERE, "pellet_watch_config.json")

# Settings saved in pellet_watch_config.json override the ones above, so this
# script can be replaced with a newer version without re-entering anything.
try:
    with open(CONFIG_PATH) as _f:
        for _k, _v in json.load(_f).items():
            if _k in ("BOILER_IP", "JSON_PORT", "JSON_PASSWORD", "WINTER_MONTHS",
                      "WINTER_ORDER", "WINTER_STOP", "SUMMER_ORDER", "SUMMER_STOP",
                      "HTML_PATH", "NTFY_TOPIC", "SERVE_PORT", "FIRST_BASELINE",
                      "MILESTONES", "DAILY_REMINDER", "REMINDER_HOUR", "ORDER_URL"):
                globals()[_k] = _v
except OSError:
    pass
if HTML_PATH is None:
    HTML_PATH = os.path.join(HERE, "pellets.html")


def _number(value):
    if isinstance(value, dict):          # some firmware wraps it as {"val": ...}
        value = value.get("val")
    return float(str(value).replace(",", ""))


def read_boiler():
    """(burner hours, burner starts). Starts is None if the boiler doesn't
    report them."""
    url = "http://%s:%s/%s/all" % (BOILER_IP, JSON_PORT, JSON_PASSWORD)
    raw = urllib.request.urlopen(url, timeout=20).read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    pe1 = json.loads(text)["pe1"]
    hours = _number(pe1["L_runtime"])
    try:
        starts = _number(pe1["L_starts"])
    except (KeyError, ValueError, TypeError):
        starts = None
    return hours, starts


def read_burner_hours():
    return read_boiler()[0]


def thresholds():
    """(order_hours, stop_hours, season name) for today's date."""
    if time.localtime().tm_mon in WINTER_MONTHS:
        return WINTER_ORDER, WINTER_STOP, "Winter"
    return SUMMER_ORDER, SUMMER_STOP, "Summer"


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"baseline": FIRST_BASELINE, "level": "ok", "history": []}
    # baseline None means "start counting from the first reading" (see run()).


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def push(title, message, click_url=""):
    if not NTFY_TOPIC:
        return
    headers = {"Title": title, "Priority": "high", "Tags": "fire"}
    if click_url:
        headers["Click"] = click_url     # tapping the notification opens it
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + NTFY_TOPIC,
            data=message.encode("utf-8"),
            headers=headers,
        )
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:               # a failed push must not stop the check
        print("ntfy push failed: %s" % e)


def hours_per_day(history):
    """Average burn rate over the stored readings (up to ~14 days)."""
    if len(history) < 2:
        return None
    (t0, h0), (t1, h1) = history[0][:2], history[-1][:2]
    days = (t1 - t0) / 86400.0
    if days < 0.5 or h1 <= h0:
        return None
    return (h1 - h0) / days


def cycling_window(cycles, end_time, days=7):
    """Starts per day and average burn minutes for the `days` before end_time.
    None until at least a day of readings is available. Burner hours are
    whole numbers, so short windows are rough."""
    pts = [c for c in cycles if c[2] is not None and c[0] <= end_time + 60]
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
    return {"days": span, "starts_per_day": d_starts / span,
            "avg_burn_min": d_hours * 60.0 / d_starts}


def cycling_rows(cycles, now_hours):
    """Table rows for the burner cycling section, or "" if no data yet."""
    if not cycles or cycles[-1][2] is None:
        return ""
    starts = cycles[-1][2]
    rows = []
    cur = cycling_window(cycles, cycles[-1][0])
    prev = cycling_window(cycles, cycles[-1][0] - 7 * 86400)

    def was(key, fmt):
        return (" <span class='was'>(week before: %s)</span>" % (fmt % prev[key])
                if prev and cur and cur["days"] >= 6 else "")
    if cur:
        label = "last %d days" % round(cur["days"]) if cur["days"] >= 1.5 else "last day"
        rows.append("<tr><td>Starts per day <span class='was'>(%s)</span></td>"
                    "<td>%.0f%s</td></tr>" % (label, cur["starts_per_day"],
                                              was("starts_per_day", "%.0f")))
        rows.append("<tr><td>Average burn <span class='was'>(%s)</span></td>"
                    "<td>%.0f min%s</td></tr>" % (label, cur["avg_burn_min"],
                                                  was("avg_burn_min", "%.0f min")))
    else:
        rows.append("<tr><td colspan='2' class='was' style='text-align:left'>Recent cycling figures appear "
                    "after a day of readings.</td></tr>")
    if starts > 0:
        rows.append("<tr><td>Average burn (lifetime)</td><td>%.0f min</td></tr>"
                    % (now_hours * 60.0 / starts))
    rows.append("<tr><td>Burner starts (lifetime)</td><td>%s</td></tr>"
                % format(starts, ",.0f"))
    return "<h2>Burner cycling</h2>\n<table>\n%s\n</table>" % "\n".join(rows)


def gauge_svg(used):
    """Fuel-gauge dial: full (green) on the right, needle moves left to amber
    at the order point and red at the hard stop."""
    import math
    order, stop, _ = thresholds()
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
    parts = [
        '<svg viewBox="0 0 300 175" class="gauge" role="img" '
        'aria-label="%.0f of %d hours used">' % (used, stop),
        arc(0, order, "#3a9a3a"),
        arc(order, stop, "#e0a100"),
        arc(stop, top, "#d23b2a"),
        '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#f2f2f2" '
        'stroke-width="7" stroke-linecap="round"/>' % (cx, cy, nx, ny),
        '<circle cx="%.1f" cy="%.1f" r="11" fill="#f2f2f2"/>' % (cx, cy),
        '<text x="265" y="172" class="tick">Full</text>',
        '<text x="%.1f" y="%.1f" class="tick">%d</text>' % (tx, ty, stop),
        '</svg>',
    ]
    return "".join(parts)


def reset_form(now_hours):
    """The "Pellets delivered" button. It posts to this script's --serve mode
    on SERVE_PORT of the same NAS, which restarts the count and sends the
    browser back to this page."""
    value = "%.0f" % now_hours if now_hours is not None else ""
    return """
<details class="reset"><summary>Pellets delivered?</summary>
<form method="post" id="resetform"
      onsubmit="return confirm('Restart the count from ' + this.hours.value + ' h?')">
<label>Burner run time at delivery
<input name="hours" type="number" inputmode="numeric" required value="%s"></label>
<button type="submit">Restart the count</button>
<p class="hint">Today's reading is filled in. Change it if the pellets came on an
earlier day.</p>
</form>
<script>
document.getElementById("resetform").action =
  location.protocol + "//" + location.hostname + ":%d/reset";
</script>
</details>""" % (value, SERVE_PORT)


def write_page(now_hours, state, level, error=None):
    if not HTML_PATH:
        return
    baseline = state["baseline"]
    used = max(0.0, now_hours - baseline) if now_hours is not None else None
    order, stop, season = thresholds()
    level = {"warn": "order"}.get(level, level)   # very old state files
    colors = {"ok": "#3a9a3a", "order": "#d99a00", "stop": "#c62828"}
    labels = {"ok": "OK", "order": "Order pellets", "stop": "OVERDUE - ORDER NOW"}
    order_button = ""
    if ORDER_URL and level in ("order", "stop"):
        order_button = ('<a class="orderbtn" href="%s" target="_blank" rel="noopener">'
                        'Order pellets</a>' % html.escape(ORDER_URL, quote=True))
    if error:
        body = "<p class='err'>Could not read the boiler: %s</p>" % html.escape(error)
    else:
        left = order - used
        rate = hours_per_day(state.get("history", []))
        eta = ""
        if rate:
            eta = "<tr><td>Burn rate</td><td>%.1f h/day</td></tr>" % rate
            if left > 0:
                days = left / rate
                when = time.strftime("%a %b %d",
                                     time.localtime(time.time() + days * 86400))
                eta += "<tr><td>Order by</td><td>about %s (%.0f days)</td></tr>" \
                       % (when, days)
        body = """
<div class="badge" style="background:%s">%s</div>
%s
<div class="big">%.0f <span>of %d h</span></div>
%s
<table>
<tr><td>Hours until order point</td><td>%s</td></tr>
<tr><td>Burner run time now</td><td>%s h</td></tr>
<tr><td>At last delivery</td><td>%s h</td></tr>
<tr><td>%s order point</td><td>%s h (%d)</td></tr>
<tr><td>Hard stop</td><td>%s h (%d)</td></tr>
%s
</table>
%s""" % (colors[level], labels[level], gauge_svg(used), used, stop, order_button,
               "%.0f h" % left if left > 0 else "now",
               format(now_hours, ",.0f"), format(baseline, ",.0f"),
               season, format(baseline + order, ",.0f"), order,
               format(baseline + stop, ",.0f"), stop, eta,
               cycling_rows(state.get("cycles", []), now_hours))
    page = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900"><link rel="apple-touch-icon" href="pellets-icon.png"><link rel="icon" href="pellets-icon.png"><meta name="apple-mobile-web-app-title" content="Pellets">
<title>Pellets</title>
<style>
body{font-family:system-ui,sans-serif;background:#2b2b2b;color:#eee;max-width:26rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.1rem;font-weight:600;color:#9c9}
h2{font-size:1rem;font-weight:600;color:#9c9;margin:1.6rem 0 0}
.was{color:#999;font-size:.85em}
.orderbtn{display:block;text-align:center;margin:.8rem 0;padding:.8rem;border-radius:.5rem;
  background:#d99a00;color:#fff;font-weight:700;font-size:1.1rem;text-decoration:none}
.badge{display:inline-block;padding:.3rem .8rem;border-radius:1rem;font-weight:700;color:#fff}
.big{font-size:3rem;font-weight:700;margin:0 0 .3rem;text-align:center}
.big span{font-size:1rem;font-weight:400;color:#aaa}
.gauge{display:block;width:100%%;max-width:20rem;margin:1rem auto 0}
.tick{fill:#aaa;font-size:13px;text-anchor:middle}
table{width:100%%;margin-top:1rem;border-collapse:collapse}
td{padding:.45rem 0;border-bottom:1px solid #444}
td:last-child{text-align:right}
.err{color:#f88}.foot{color:#888;font-size:.8rem;margin-top:1rem}
.reset{margin-top:1.5rem;background:#333;border-radius:.6rem;padding:.7rem .9rem}
.reset summary{cursor:pointer;font-weight:600;color:#9c9}
.reset label{display:block;margin:.8rem 0 .4rem;color:#ccc}
.reset input{display:block;width:100%%;box-sizing:border-box;margin-top:.3rem;padding:.5rem;
  font-size:1.2rem;background:#222;color:#eee;border:1px solid #555;border-radius:.4rem}
.reset button{width:100%%;padding:.7rem;font-size:1rem;font-weight:700;border:0;
  border-radius:.4rem;background:#3a9a3a;color:#fff}
.hint{color:#999;font-size:.8rem}
</style></head><body>
<h1>Pellet boiler &ndash; hours since last delivery</h1>
%s
%s
<p class="foot">Checked %s</p>
</body></html>""" % (body, reset_form(now_hours), time.strftime("%Y-%m-%d %H:%M"))
    try:
        tmp = HTML_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp, HTML_PATH)
    except OSError as e:
        print("Could not write %s: %s" % (HTML_PATH, e))


def run(delivered=False, new_baseline=None):
    """One check. With delivered=True, first restart the count from
    new_baseline (or from the boiler's current reading when None)."""
    state = load_state()

    starts = None
    try:
        now_hours, starts = read_boiler()
    except Exception as e:
        if new_baseline is None:
            print("Could not read the boiler: %s" % e)
            write_page(None, state, state.get("level", "ok"), error=str(e))
            return 2
        now_hours = new_baseline

    if state.get("baseline") is None and not delivered:
        state["baseline"] = now_hours
        print("First run: counting from the current reading (%.0f h). If your last "
              "delivery was earlier, run with --delivered <hours at delivery>." % now_hours)

    if delivered:
        state["baseline"] = new_baseline if new_baseline is not None else now_hours
        state["level"] = "ok"
        state["history"] = []
        state["notified"] = []
        state.pop("last_reminder", None)
        print("Count restarted. Baseline is now %.0f h; order at %.0f h."
              % (state["baseline"], state["baseline"] + thresholds()[0]))

    order, stop, season = thresholds()
    used = now_hours - state["baseline"]
    left = order - used
    level = "stop" if used >= stop else "order" if used >= order else "ok"

    history = state.get("history", [])
    history.append([time.time(), now_hours])
    cutoff = time.time() - 14 * 86400
    state["history"] = [h for h in history if h[0] >= cutoff][-400:]

    # Burner starts, kept across deliveries (60 days) for the cycling figures.
    if starts is not None:
        cycles = state.get("cycles", [])
        cycles.append([time.time(), now_hours, starts])
        cutoff = time.time() - 60 * 86400
        state["cycles"] = [c for c in cycles if c[0] >= cutoff][-1500:]

    summary = "%.0f burner hours used since the last delivery; %s order point is " \
              "%d h, hard stop %d h. Boiler reads %.0f h." \
              % (used, season.lower(), order, stop, now_hours)
    alert = pick_alert(state, used, order, stop, level)
    state["level"] = level
    save_state(state)
    write_page(now_hours, state, level)

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
    return 0


def pick_alert(state, used, order, stop, level):
    """The alert to send on this check, or None. Updates state["notified"].
    Heads-up milestones, the order point and the hard stop are each sent once
    per delivery; past the order point a daily reminder follows if enabled."""
    marks = [("%d%%" % round(f * 100), f * order) for f in sorted(MILESTONES)]
    marks += [("order", order), ("stop", stop)]
    reached = [key for key, hours in marks if used >= hours]
    if "notified" not in state:
        # First run with this version: don't send alerts for points passed
        # before it was installed, except the order point and hard stop.
        state["notified"] = [k for k in reached if k not in ("order", "stop")]
    new = [k for k in reached if k not in state["notified"]]
    state["notified"] += new
    today = time.strftime("%Y-%m-%d")

    if "stop" in new or (level == "stop" and new):
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
        return "Reminder: time to order pellets (%.0f of %d h)." % (used, order)
    return None


class ResetHandler(BaseHTTPRequestHandler):
    def _send(self, code, text, location=None):
        data = text.encode("utf-8")
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        length = min(int(self.headers.get("Content-Length") or 0), 1000)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        try:
            hours = float(form.get("hours", [""])[0].replace(",", ""))
            if hours <= 0:
                raise ValueError
        except ValueError:
            return self._send(400, "<p>Please enter the burner hours as a number. "
                                   "<a href='javascript:history.back()'>Back</a></p>")
        run(delivered=True, new_baseline=hours)

        # Back to the page the button was on (only on this same host).
        back = self.headers.get("Referer", "")
        host = (self.headers.get("Host") or "").split(":")[0]
        if urllib.parse.urlsplit(back).hostname != host or not host:
            back = "/pellets.html"
        self._send(303, "<p>Count restarted.</p>", location=back)

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))


def serve():
    print("Pellet watch: page and reset button on port %d" % SERVE_PORT)
    sys.stdout.flush()
    HTTPServer(("", SERVE_PORT), ResetHandler).serve_forever()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--serve":
        serve()
        return 0
    if args and args[0] == "--delivered":
        return run(True, float(args[1].replace(",", "")) if len(args) > 1 else None)
    return run()


if __name__ == "__main__":
    sys.exit(main())
