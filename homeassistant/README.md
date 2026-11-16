# Home Assistant version

The same pellet reorder reminder as `pellet_watch.py`, built entirely in Home Assistant. Use this instead of the script if you already run HA.

## What you get

- **Pellet hours since delivery:** a sensor, with the season's order point and hard stop as attributes
- **Pellet status:** a sensor reading `ok`, `order` or `stop`
- **An alert** when the order point or hard stop is reached, repeated daily at 9:00 until the next delivery. It appears as an HA notification and, optionally, on your phone.
- **A "Pellets delivered" script** that restarts the count, plus a dashboard card with a gauge and a button

## Requirements

- The [oekofen-pellematic-compact](https://github.com/dominikamann/oekofen-pellematic-compact) integration, installed via HACS and connected to your boiler's JSON interface
- Home Assistant 2024.10 or newer (the file uses the current `triggers:` / `actions:` syntax)

## Setup

### 1. Find your burner runtime sensor

In **Settings → Devices & services → Entities**, search for `runtime`. You want the PE1 burner run time in hours: the integration's sensor for `pe1 L_runtime`. Its value should match *PE1 Burner Run Time* on the touchscreen or in the myPelletronic app. Copy its entity ID, e.g. `sensor.pellematic_pe1_runtime`.

### 2. Turn on packages

If your `configuration.yaml` doesn't already have it, add:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

and create a `packages` folder next to `configuration.yaml`.

### 3. Add the package

Copy [`pellet_watch_package.yaml`](pellet_watch_package.yaml) to `/config/packages/pellet_watch.yaml`. Then edit two places:

- **Required:** in the "Pellet burner run time" sensor, replace both instances of `sensor.pellematic_pe1_runtime` with your entity ID from step 1.
- **Optional:** in the automation, change `notify.notify` to your phone, e.g. `notify.mobile_app_your_phone`.

The order points live in the `order_point` and `hard_stop` attributes: 650 / 700 h in winter (October–April) and 675 / 700 h in summer. See [Choosing your thresholds](../README.md#choosing-your-thresholds) in the main README.

### 4. Restart and set your starting point

Restart Home Assistant (Developer tools → YAML → Check configuration first). Then go to **Developer tools → Actions**, choose **Script: Pellets delivered**, enter your burner hours at the last delivery, and click **Perform action**.

### 5. Add the dashboard card

In a dashboard, choose **Edit → Add card → Manual**, then paste [`dashboard_card.yaml`](dashboard_card.yaml).

## After a pellet delivery

Tap the **Pellets delivered** button on the card and confirm. That uses the boiler's current reading. To use the reading from an earlier day, run the script from Developer tools with the `hours` field filled in.
