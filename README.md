# Ökofen Pellet Boiler – pellet reorder reminder

Know when to order pellets for an **ÖkoFEN Pellematic** boiler, without extra hardware.

`pellet_watch.py` reads the boiler's **burner run time** over its local JSON interface, counts the hours since your last pellet delivery, and:

- shows a fuel-gauge status page you can add to your phone's home screen
- alerts you (email or phone push) at a quarter, half and three-quarters of the way to the order point, then at the order point and the hard stop, with a daily reminder until the next delivery; the order point is earlier in winter
- has a **"Pellets delivered?"** button on the page to restart the count after a refill
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
- `pellet_watch_config.json`: a copy of `pellet_watch_config.example.json`, renamed and edited

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
| `MILESTONES` | Heads-up alerts as fractions of the order point, each sent once per delivery [`[0.25, 0.5, 0.75]`; `[]` = none] |
| `DAILY_REMINDER` / `REMINDER_HOUR` | Repeat the alert daily after the order point, at the first check from this hour on [true / 8] |
| `NTFY_TOPIC` | Optional phone push via [ntfy.sh](https://ntfy.sh): a hard-to-guess topic name [off] |

Your settings live in the config file, so you can drop in a newer `pellet_watch.py` without re-entering anything.

Run it once by hand:

```bash
python3 pellet_watch.py
```

It prints something like `629 burner hours used since the last delivery; winter order point is 650 h, hard stop 700 h.`

If your last delivery was earlier, tell it the reading from that day:

```bash
python3 pellet_watch.py --delivered 12543
```

## 3a. Synology NAS

Put the files in the `web` shared folder (Web Station serves it as `http://YOUR-NAS/`). SSH in and test with `python3 /volume1/web/pellet_watch.py`.

In **Control Panel → Task Scheduler** create two tasks:

1. **Scheduled Task → User-defined script**: daily or every few hours.
   - Run command: `python3 /volume1/web/pellet_watch.py`
   - Task Settings: tick **Send run details by email** and **only when the script terminates abnormally**. The script exits with an error code only on checks that raise an alert (each heads-up milestone, the order point, the hard stop, then once a day until you restart the count), so DSM emails you then and stays quiet otherwise. The first line of the email says which alert it is. (Control Panel → Notification → Email must be set up.)
2. **Triggered Task → User-defined script**, Event **Boot-up**, user `root`.
   - Run command: `python3 /volume1/web/pellet_watch.py --serve`
   - Select it and click **Run** once to start it now. This powers the "Pellets delivered?" button.

Page: `http://YOUR-NAS/pellets.html`. If the DSM firewall is on, allow port 8765 from your LAN.

> Anything in `web` can be fetched by a browser. If your NAS web server is reachable from the internet, keep the script and config in another shared folder and point `HTML_PATH` at the `web` folder.

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
```

## Check that it's working

Run these on the machine running the script, from the script's folder (on a Synology NAS: `cd /volume1/web` after logging in with SSH).

**When the page was last updated:**

```bash
grep -o 'Checked [0-9-]* [0-9:]*' pellets.html
```

With a check every 6 hours, this should never be more than 6 hours old.

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
| 0 | Fine |
| 1 | Alert on this check: a heads-up milestone, the order point, the hard stop, or the daily reminder after the order point |
| 2 | Couldn't read the boiler |

## Security notes

- The reset button has no password: anyone on your home network who opens the page can press it. Don't forward its port to the internet.
- The JSON password is in `pellet_watch_config.json`. Keep that file out of public web folders where possible.

## Credits

- Field names from the community [Ökofen JSON documentation](https://github.com/thannaske/oekofen-json-documentation)
- Home Assistant version built on the [oekofen-pellematic-compact](https://github.com/dominikamann/oekofen-pellematic-compact) integration
- Not affiliated with or endorsed by ÖkoFEN. "ÖkoFEN" and "Pellematic" are trademarks of their owner.

## License

MIT, see [LICENSE](LICENSE).
