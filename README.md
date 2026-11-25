# Ökofen Pellet Boiler – pellet reorder reminder

Know when to order pellets for an **ÖkoFEN Pellematic** boiler, without extra hardware.

`pellet_watch.py` reads the boiler's **burner run time** over its local JSON interface, counts the hours since your last pellet delivery, and:

- shows a fuel-gauge status page you can add to your phone's home screen
- alerts you (email or phone push) at a quarter, half and three-quarters of the way to the order point, then at the order point and the hard stop, with a daily reminder until the next delivery; the order point is earlier in winter
- has a **"Pellets delivered?"** button on the page to restart the count after a refill, and an **"I've ordered pellets"** button that pauses the daily reminders until the delivery
- shows the **outdoor temperature** from the boiler's own online weather service (no outdoor sensor needed), heating degree-days and burner hours per degree-day, and an **advisory** boiler setpoint from an outdoor-temperature curve (nothing is ever written to the boiler)
- can log the boiler's temperature, setpoint, state and modulation **every minute** to a CSV file, to see what's behind short burns
- tracks **burner cycling**: starts per day and average burn length over the last week, compared with the week before (useful when tuning thermostats or boiler settings to reduce short cycling)

<img src="docs/screenshot.png" width="320" alt="Status page with a fuel gauge, hours until the order point, and the Pellets delivered button">

## Why burner hours?

Pellet use tracks burner run time closely. For a hopper that's refilled when nearly empty, the hours between deliveries are a reliable "fuel gauge". Ökofen sells weighing cells for the storage tank that do this properly. This project is the free, software-only version for everyone else.

> **Using Home Assistant?** There's an HA-only version (sensors, alert automation, dashboard card) in [`homeassistant/`](homeassistant/README.md). No script needed.

## What you need

- An ÖkoFEN boiler with a **Pelletronic Touch** controller on your home network
- An always-on machine on the same network with **Python 3** (no extra packages):
  a **Synology NAS**, a **Raspberry Pi** or other Linux box, or a Windows/Mac computer that doesn't sleep

The boiler's JSON interface only answers on your home network, so the script has to run there. It does **not** use your my.oekofen.info login.

## 1. Turn on the boiler's JSON interface

On the Pelletronic Touch screen, unlock the advanced mode (lock icon), then go to **General → IP Config** (network settings) and scroll to the bottom:

- **JSON mode / JSON interface:** On (leave compatibility mode off)
- Note the **JSON port** (usually `4321`) and the **JSON password**
- Note the boiler's **IP address**. Giving it a fixed address (a DHCP reservation in your router) saves trouble later.

Test it from any browser on your network (fill in your values):

```
http://BOILER_IP:4321/JSON_PASSWORD/all
```

You should see a page of JSON. Look for `pe1` → `L_runtime`: that's the burner run time in hours, the same number the touchscreen and app show as *PE1 Burner Run Time*.

## 2. Install and configure

Put these files in one folder on the always-on machine:

- `pellet_watch.py`
- `pellets-icon.png`
- `pellet_watch_config.json`: a copy of `pellet_watch_config.example.json`, renamed and edited (the example is filled in for a Synology NAS and a boiler set to °F; the defaults in the settings table apply to anything you leave out)

Minimum config:

```json
{
  "BOILER_IP": "192.168.1.50",
  "JSON_PORT": 4321,
  "JSON_PASSWORD": "your-json-password"
}
```

Optional keys (defaults in brackets):

| Key | Meaning |
|---|---|
| `FIRST_BASELINE` | Burner hours at your last delivery, used on the first run only [count from now] |
| `WINTER_MONTHS` | Months that use the winter thresholds [`[10,11,12,1,2,3,4]`] |
| `WINTER_ORDER` / `WINTER_STOP` | Winter order point / hard stop, in hours since delivery [650 / 700] |
| `SUMMER_ORDER` / `SUMMER_STOP` | Summer order point / hard stop [675 / 700] |
| `HTML_PATH` | Where to write the status page [`pellets.html` next to the script; `""` = none] |
| `SERVE_PORT` | Port for the page's reset button and built-in web server [8765] |
| `STALE_HOURS` | The page shows a warning if it hasn't been updated for this long [13] |
| `MILESTONES` | Heads-up alerts as fractions of the order point, each sent once per delivery [`[0.25, 0.5, 0.75]`; `[]` = none] |
| `DAILY_REMINDER` / `REMINDER_HOUR` | Repeat the alert daily after the order point, at the first check from this hour on [true / 8] |
| `ORDER_URL` | Your supplier's ordering page: added to order alerts and shown as an "Order pellets" button on the page once it's time [none] |
| `TEMP_UNIT` | `"C"` or `"F"` for temperatures on the page [`"C"`] |
| `ADVISORY` | Show the advisory setpoint [true] |
| `ADVISORY_COLD_OUTDOOR` / `ADVISORY_COLD_BOILER` | Curve point for cold weather, °C: boiler setpoint at this outdoor temperature [-10 / 74] |
| `ADVISORY_MILD_OUTDOOR` / `ADVISORY_MILD_BOILER` | Curve point for mild weather, °C [10 / 65] |
| `DEGREE_DAY_BASE` | Base for heating degree-days, °C [18.3, i.e. 65 °F] |
| `SETPOINT_HINT` | Push (high priority) and email a suggestion to change the boiler's setpoint by hand when the advisory setpoint, from the colder of the boiler's online forecast for the coming day and the last day's average outdoor temperature, is at least `SETPOINT_HINT_DIFF` away from it. So it suggests raising the setpoint before a cold snap, and lowering it only once it has been mild and is forecast to stay mild. Suggestions are rounded to 5 °F (or 1 °C); at most one per `SETPOINT_HINT_DAYS`, and not repeated until the suggestion changes. Needs at least 3 checks a day (the 6-hourly check gives 4) [false] |
| `SETPOINT_HINT_DIFF` / `SETPOINT_HINT_DAYS` | Difference that triggers a hint, K / days of past outdoor temperature averaged [2.8 (5 °F) / 1] |
| `SETPOINT_HINT_MIN` | Lowest setpoint a hint may suggest, °C, e.g. 68.3 (155 °F) so an indirect hot-water tank still reheats well [none] |
| `LOG_SECONDS` | `--serve` logs boiler readings this often to `pellet_watch_log_YYYY-MM.csv` [0 = off; 60 = every minute] |
| `LOG_KEEP_MONTHS` | Months of log files to keep [3] |
| `WEATHER_UNIT` | Unit the boiler reports the online weather temperature in [`"C"`; `"F"` on imperial-unit boilers] |
| `TEMP_FACTOR` | Units of the boiler's temperatures [0.1, i.e. tenths of a degree] |
| `BEESTAT_API_KEY` | Optional: a [beestat.io](https://beestat.io) API key, to save your ecobee thermostats' heat-call records [off] |
| `ORDERED_QUIET_DAYS` | After you press **I've ordered pellets** on the page (or run `--ordered`), the daily reminders pause until the delivery is recorded, or for this many days, after which they resume. The hard-stop alert is always sent. `--ordered cancel` or **Not ordered after all** resumes them [14] |
| `ALLOWED_HOSTS` | Extra names the page server answers to, e.g. `["nas.local"]`. IP addresses, `localhost`, the machine's own name and the `PAGE_URL` host always work [none] |
| `DATA_DIR` | Folder for the CSV logs (boiler log, ecobee files, ecobee history), relative to the script or a full path. Existing logs are moved into it on the next run [next to the script] |
| `NTFY_TOPIC` | Optional phone push via [ntfy.sh](https://ntfy.sh): a hard-to-guess topic name [off] |
| `NTFY_SERVER` | ntfy server, if you run your own [`https://ntfy.sh`] |
| `SMTP_USER` / `SMTP_PASSWORD` | Optional: the script emails alerts itself from this account, using an **app password** [off]. See [Email alerts](#email-alerts-optional) |
| `SMTP_HOST` / `SMTP_PORT` | Mail server; port 465 uses SSL, 587 STARTTLS [`smtp.mail.yahoo.com` / 465; Gmail: `smtp.gmail.com` / 465] |
| `MAIL_TO` | Who gets the alert emails: an address or a list [the sending account] |
| `PAGE_URL` | Your status page's address, linked in alert emails and pushes [none] |

### Email alerts (optional)

DSM's Task Scheduler email wraps alerts in its own template ("Task Scheduler has completed a scheduled task", start/stop times, "Current status: 1 (Interrupted)"), which can't be changed. For a plain email with a subject like **"Time to order pellets - order point of 650 h reached"**, let the script send it:

1. Create an **app password** for the sending account. Yahoo: Account security → Generate app password. Gmail: turn on 2-step verification, then myaccount.google.com/apppasswords. **Outlook.com / Hotmail won't work**: Microsoft no longer accepts passwords (including app passwords) for sending mail from scripts.
2. Add to `pellet_watch_config.json`:
   ```json
   "SMTP_USER": "you@yahoo.com",
   "SMTP_PASSWORD": "the-app-password",
   "MAIL_TO": ["you@outlook.com"],
   "PAGE_URL": "http://your-nas/pellets/pellets.html"
   ```
3. Test it: `python3 pellet_watch.py --test-notify` (sends a test email, and a test push if `NTFY_TOPIC` is set).

When the email goes out, the check exits 0, so the DSM email is left for real problems only (boiler unreachable, bad settings). If the email fails, the check exits 1 as before and DSM still emails you.

For a phone notification as well, install the **ntfy** app, subscribe to a hard-to-guess topic name, and set `NTFY_TOPIC` to the same name. Tapping the notification opens the order page; it also has a "Status page" button when `PAGE_URL` is set. Anyone who knows the topic name can read it, so don't use an obvious one.

Your settings live in the config file, so you can drop in a newer `pellet_watch.py` without re-entering anything. Misspelled settings are reported as a warning, and impossible ones (an order point above the hard stop, a port in quotes) stop the script with exit code 2.

Tell it the burner reading from your last delivery (or leave out the number to start counting from now):

```bash
python3 pellet_watch.py --delivered 12543
```

To check where things stand at any time without using up an alert (it only creates `DATA_DIR` and moves old CSVs into it, like any run):

```bash
python3 pellet_watch.py --status
```

Use `--status` for checks by hand. A plain `python3 pellet_watch.py` is the scheduled check: if it crosses an alert point, that alert is printed to your terminal instead of being emailed.

## 3a. Synology NAS

Keep the script and its config **out of** the `web` folder (anything in `web` can be read by any browser on your network, including the JSON password) and write only the page there:

```
scripts (shared folder)         web (Web Station's folder)
└── pellet_watch                └── pellets
    ├── pellet_watch.py             ├── pellets.html   (written by the script)
    ├── pellet_watch_config.json    └── pellets-icon.png
    ├── pellets-icon.png
    └── (state files, created by the script)
```

1. In **Control Panel → Shared Folder**, create a shared folder called `scripts`. In File Station, make `scripts/pellet_watch` and `web/pellets`.
2. Put `pellet_watch.py`, `pellets-icon.png` and your `pellet_watch_config.json` in `scripts/pellet_watch`, and **also copy `pellets-icon.png` into `web/pellets`** (the page looks for the icon next to itself).
3. Add the page's location to your config: `"HTML_PATH": "/volume1/web/pellets/pellets.html"`
4. SSH in and run `python3 /volume1/scripts/pellet_watch/pellet_watch.py --delivered <hours at your last delivery>`.

In **Control Panel → Task Scheduler** create two tasks:

1. **Scheduled Task → User-defined script**: every 6 hours (first run 00:00, last 18:00).
   - Run command: `python3 /volume1/scripts/pellet_watch/pellet_watch.py`
   - Task Settings: tick **Send run details by email** and **only when the script terminates abnormally**. The script exits with an error code only on checks that raise an alert (each heads-up milestone, the order point, the hard stop, then once a day until you restart the count), so DSM emails you then and stays quiet otherwise. The first line of the email says which alert it is. With [script email alerts](#email-alerts-optional) set up, alerts come in the script's own plain email instead and DSM emails only on real problems. (Control Panel → Notification → Email must be set up.)
2. **Triggered Task → User-defined script**, Event **Boot-up**.
   - Run command: `python3 /volume1/scripts/pellet_watch/pellet_watch.py --serve`
   - Select it and click **Run** once to start it now. This powers the "Pellets delivered?" button. The buttons always run the current `pellet_watch.py`, so updating the script doesn't need a restart for them. The minute logger reloads the settings file when it changes. Restart this task after changing `SERVE_PORT`, `HTML_PATH`, `PAGE_URL` or `ALLOWED_HOSTS`, or after updating `pellet_watch.py` if you want the logger and the page server to run the new code.

For both tasks, the safest user is a dedicated non-admin DSM account (e.g. `pelletwatch`) with read/write access to `scripts` and `web` only. `root` works but gives the script more access than it needs.

Page: `http://YOUR-NAS/pellets/pellets.html`. If the DSM firewall is on, allow port 8765 from your LAN. The button posts to plain `http://`, so open the page over `http://` too (an `https://` page will warn about or block the form).

## 3b. Raspberry Pi / Linux

The `--serve` mode also serves the page, so no web server is needed. The page is at `http://PI-ADDRESS:8765/`.

Hourly check with cron (`crontab -e`), adjusting the path:

```
0 * * * * /usr/bin/python3 /home/pi/pellets/pellet_watch.py
```

Keep `--serve` running with systemd. Create `/etc/systemd/system/pellet-watch.service`:

```ini
[Unit]
Description=Pellet watch page and reset button
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/pellets/pellet_watch.py --serve
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now pellet-watch
```

For alerts, the easiest option on a Pi is phone push: set `NTFY_TOPIC` and subscribe to that topic in the ntfy app.

## 3c. Windows

Install Python from python.org, then in **Task Scheduler**:

- A task that runs `python C:\pellets\pellet_watch.py` every hour
- A task **At startup** that runs `python C:\pellets\pellet_watch.py --serve`

Page: `http://THIS-PC:8765/`. Allow Python through Windows Firewall when asked. Use `NTFY_TOPIC` for alerts.

## After a pellet delivery

Open the page, tap **Pellets delivered?**, check the reading (it's pre-filled with today's), and tap **Restart the count**.

Or from a terminal:

```bash
python3 pellet_watch.py --delivered            # use the boiler's current reading
python3 pellet_watch.py --delivered 13250      # or the reading on delivery day
python3 pellet_watch.py --undo                 # undo the last restart
```

Readings that can't be right (higher than the boiler's current reading, far in the past, or not a number) are refused. Each restart is logged in the state file with the old and new readings, which is what `--undo` uses.

## Check that it's working

Run these on the machine running the script, from the script's folder (on a Synology NAS: `cd /volume1/scripts/pellet_watch` after logging in with SSH).

**Current status, without changing anything:**

```bash
python3 pellet_watch.py --status
```

**When the page was last updated:** the bottom of the page says "Checked …". If the checks stop, the page shows a red warning once it's more than 13 hours old (`STALE_HOURS`).

**Recent readings, with times:**

```bash
python3 -c "import json,time;[print(time.strftime('%Y-%m-%d %H:%M',time.localtime(t)),'%.0f h'%h) for t,h in json.load(open('pellet_watch_state.json'))['history'][-10:]]"
```

You should see entries at your scheduled times. If the only entries are from when you ran the script by hand, the scheduled check isn't running.

**The reset-button helper:**

```bash
ps aux | grep "[p]ellet_watch.py --serve"
```

One line of output means it's running. From another computer, `http://YOUR-NAS:8765/` should load.

**On a Synology NAS**, Task Scheduler should list **two** pellet tasks:

| Task | Next run time |
|---|---|
| The check (`pellet_watch.py`) | a time later today |
| The button helper (`pellet_watch.py --serve`) | Boot-up |

A common mistake is to create only the button helper. Everything looks fine at first, but the page never updates and no alert is ever sent.

**Email:** in DSM, **Control Panel → Notification → Email → Send a test email**. If that arrives, the pellet alert will too.

## Weather, degree-days and the advisory setpoint

The Pelletronic Touch fetches an online weather forecast for your location (Main menu → Weather), and the JSON interface reports its current temperature as `weather.L_temp`. pellet_watch uses that, so no outdoor sensor is needed. The page then shows:

- **Outdoor temperature** and **heating degree-days** over the last week. Degree-days measure how cold it was: one degree below 65 °F for a whole day is one degree-day.
- **Burner hours per degree-day**: burner hours divided by degree-days. Unlike pounds per burner hour, this measures efficiency fairly across mild and cold weeks, so it shows whether a change (thermostat settings, a buffer tank, a new boiler setting) actually helped.
- **Suggested setpoint (advisory)**: the boiler temperature a simple outdoor-temperature curve would choose, alongside the boiler's actual setpoint. By default the curve runs from 74 °C at -10 °C outside to 65 °C at +10 °C outside. **Nothing is sent to the boiler.** It's there to collect evidence before you discuss outdoor reset with your installer, who should confirm the lowest safe boiler temperature for your system.

Run `python3 pellet_watch.py --status` once to check the readings: the outdoor temperature should match the Weather page in the app, and the boiler temperature the app's boiler page. If they're out by a factor of ten, set `TEMP_FACTOR` to 1. On boilers set to imperial units, the online weather temperature comes through in °F while boiler temperatures stay in °C: if the outdoor reading looks like the °F number treated as °C (e.g. 88 °F on a 31 °F morning), set `"WEATHER_UNIT": "F"`.

### Minute-by-minute log

With `"LOG_SECONDS": 60`, the `--serve` process also logs a row every minute to `pellet_watch_log_YYYY-MM.csv` in `DATA_DIR` (next to the script if unset): outdoor temperature, boiler temperature and setpoint, combustion chamber temperature, modulation, boiler state, burner hours and starts, and the advisory setpoint. A week of this shows whether burns end because a zone stopped calling or because the boiler reached its switch-off temperature. Changes to `LOG_SECONDS`, `DATA_DIR` and the boiler settings are picked up within a minute. The log adds one request a minute to the boiler's JSON interface.

### ecobee heat calls (optional, via beestat)

The boiler can't tell you which zone asked for heat: every call arrives as the same burner contact. If your zones have ecobee thermostats, their records can. ecobee no longer issues API keys, but the free service [beestat.io](https://beestat.io) reads ecobee accounts and has its own API.

1. Sign in at beestat.io with your ecobee account, then in beestat's settings, under **Manage API Key**, create a key. Anyone with the key can read your thermostat data, so keep it private.
2. Add it to your config: `"BEESTAT_API_KEY": "your-key"`.

Each scheduled check then asks beestat to sync and appends any new 5-minute records to `pellet_watch_ecobee_YYYY-MM.csv`: time, thermostat, seconds of heat call and fan in that 5 minutes, indoor temperature, heat setpoint, outdoor temperature and comfort setting (Home, Away, Sleep). The first run fetches the past week. Put that file next to the minute log, and every burn can be matched to a zone's call, or to none of them (hot water, or a zone without an ecobee). A beestat problem never changes the check's exit code; it's only noted in the output.

### Last winter's ecobee data (one-off)

beestat keeps about 13 months of 5-minute ecobee records. To save the older ones before they expire:

```bash
python3 pellet_watch.py --ecobee-history 2025-11-01 2026-04-30
```

This writes `pellet_watch_ecobee_history_YYYY-MM.csv`, one file per month for all thermostats, in the same format as the regular ecobee files. Re-running rewrites them; they are never deleted automatically.

### Boiler error alert

With the minute logger running (`LOG_SECONDS`, `--serve`), the script emails
and pushes once when the boiler has shown its error state for
`ERROR_ALERT_MINUTES` (default 3), and pushes again after it has been running
again for 5 minutes. If neither email nor push can be sent, it tries again the
next minute. The scheduled check repeats the test as a backup; without the
minute logger it alerts the first time it sees the error (and exits 1 if it
could not send, so DSM emails you). The error code itself is in
myPelletronic under General > Info. Set `"ERROR_ALERT": false` to turn it off.

### Boiler outages are left out of the figures

With the minute logger running, the page leaves boiler outages out of the burn
rate, starts per day, average burn and burner hours per degree-day, so a day
of repairs doesn't skew them or push the "order by" date out. An outage runs
from the first error reading, or the last reading before 30+ minutes with
none (boiler switched off or unreachable), until the burner has run steadily
for 10 minutes again; stops under 30 minutes are ignored. It is worked out
from the minute logs each time the page is built, so earlier outages count
too, and the page says how many hours were left out.

### ecobee heat dump (optional, trial)

On hydro-air systems the boiler pump stops about a minute after the last call
ends while the burner is still burning off, so the trapped water can overshoot
to 190 degF or more. With `ECOBEE_DUMP` on, when the boiler water passes
`ECOBEE_DUMP_START_C` (181 degF) with the burner not firing and no ecobee
calling, the script raises one ecobee's heat setpoint by `ECOBEE_DUMP_BUMP_F`
for up to `ECOBEE_DUMP_MINUTES`. That zone opens, the pump and the air-handler
fan run, and the heat goes into the house. The hold is released when the water
is down to `ECOBEE_DUMP_STOP_C` (170 degF), at the time limit, if the burner fires, or if
the water has not started cooling after 8 minutes (the zone never opened;
ecobee's reported status lags by minutes, so the water is the better test). The hold is set 3 degF above
the room temperature (or the setpoint, if higher) so the zone really calls,
and it has its own end time, so the thermostat goes back to its schedule even
if the NAS stops. The script only resumes the schedule if its own hold is
still the one in force, so a change someone makes at the thermostat during a
dump is left alone. No dump starts while the water is already falling (a zone,
usually the tank, is taking the heat). At most
`ECOBEE_DUMP_MAX_PER_DAY` dumps a day; none while the boiler shows an error or
the thermostat is not in heat or auto mode. Each start, end and skip is logged
to `pellet_watch_dump_YYYY-MM.csv` (with the peak temperature).

ecobee no longer issues developer keys, so this uses the same sign-in as Home
Assistant's ecobee integration. Once, on your own computer:

```
pip install python-ecobee-api      # needs Python 3.9 or newer on that computer
python tools/ecobee_login.py
```

It asks for your ecobee email, password and (if set) the two-step code, and
writes `pellet_watch_ecobee_token.json`, a sign-in token only; the password is
not saved. Copy it beside `pellet_watch.py` on the NAS (not into the web
folder) and delete your local copy. Then set `"ECOBEE_DUMP": true` and
`"ECOBEE_DUMP_THERMOSTAT"` to the thermostat's name (required). Check with:

```
python3 pellet_watch.py --ecobee-status     # read-only
python3 pellet_watch.py --ecobee-dump-test  # a real 3-minute hold
```

After each dump you get a quiet, low-priority push saying how long it ran,
the boiler temperature at the start, peak and end, and why it ended; turn it
off with `"ECOBEE_DUMP_NOTIFY": false`. `"ECOBEE_DUMP_DRY_RUN": true` logs what
it would do without touching the thermostat. If the script cannot reach the thermostat (a rejected token,
ecobee or the internet down) you get one push with the reason; for a rejected
token it asks you to run `tools/ecobee_login.py` again. Changing your ecobee password revokes it.

## Choosing your thresholds

You want the pellets to arrive before the hopper runs dry. Three numbers decide it:

1. **Hopper capacity** in lbs or kg
2. **Pellets burned per burner hour**: pounds delivered ÷ burner hours since the previous delivery. Log this at each delivery; it varies a lot (7.5–9.5 lbs/h on the boiler this was built for).
3. **Delivery lead time** × **burner hours per day**: 7–10 days at 8–10 h/day in winter is ~100 h; summer is often only 3–4 h/day.

**Hours to empty** ≈ capacity ÷ lbs per hour. **Order point** ≈ hours to empty − lead-time hours. Example: a 6,600 lb hopper at 8.5 lbs/h runs dry around 775 h, so with ~100 h of winter lead time, order by ~675 h. At 9.5 lbs/h it runs dry around 695 h, so order by ~600 h.

Also check your supplier's minimum delivery: ordering too early may mean a small-delivery fee.

The page shows your measured burn rate (h/day) and an estimated "Order by" date once it has a few days of readings.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Fine, nothing to report, or an alert the script emailed itself (`SMTP_USER` set) |
| 1 | Alert on this check that the script did not email (a heads-up milestone, the order point, the hard stop, or the daily reminder after it), or the very first run |
| 2 | A problem: the boiler couldn't be read after 3 tries, a setting is wrong, the state file is damaged (it is left untouched, with a `.bak` copy of the last good one), the page couldn't be written, or an unexpected error |

## Security notes

- The page buttons have no password: anyone on your home network who opens the page can press them. The server only answers requests addressed to this machine by an IP address, its own name, the `PAGE_URL` host or a name in `ALLOWED_HOSTS` (which blocks DNS-rebinding tricks), refuses cross-site and `Origin: null` posts, always redirects back to `PAGE_URL`, refuses impossible readings, and reports every restart by email and push (`--undo` reverses one). Don't forward its port to the internet.
- The JSON password, the email app password and the ntfy topic are in `pellet_watch_config.json`. Keep that file out of any web folder (see the Synology layout above). The passwords are never written to the page or to error messages. Use an app password, never your main account password; you can revoke it at any time.

## Credits

- Field names from the community [Ökofen JSON documentation](https://github.com/thannaske/oekofen-json-documentation)
- Home Assistant version built on the [oekofen-pellematic-compact](https://github.com/dominikamann/oekofen-pellematic-compact) integration
- Not affiliated with or endorsed by ÖkoFEN. "ÖkoFEN" and "Pellematic" are trademarks of their owner.

## License

MIT, see [LICENSE](LICENSE).
