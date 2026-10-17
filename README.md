# Stadtwerke Elbtal for Home Assistant

Home Assistant integration for the **Stadtwerke Elbtal** customer portal
(`onlineservice.stadtwerke-elbtal.de`). It reads electricity meter readings from the
portal and imports them into the Energy Dashboard.

Up to version 0.1 this integration was called **ITC PowerCommerce** and talked to the
portal the utility ran on that platform. In October 2026 Stadtwerke Elbtal moved its
portal to Salesforce; version 0.2 follows it there and was renamed with it. See
[Upgrading from ITC PowerCommerce](#upgrading-from-itc-powercommerce-01).

## What it does

- **Meter reading sensor** — the current cumulative reading in kWh
  (`device_class: energy`, `state_class: total_increasing`), grouped under a device
  named after the meter. Attributes:
  - `meter_number`
  - `reading_date` — the date of the reading, not of the poll
  - `reading_source` — `read`, `estimated` or `self_reported`, where the portal
    discloses it
- **History backfill** — every reading the portal keeps (several years' worth) is
  imported as an external statistic named **"Strom &lt;meter number&gt;"**
  (`stadtwerke_elbtal:<meter number>_energy`), so the Energy Dashboard shows
  consumption from before the day you set the integration up. Readings that later
  drop out of the portal's window stay in Home Assistant.
- **Two polling rhythms** — the current reading needs one short request per contract
  and is fetched every 12 hours; the history pages through all readings and is
  fetched once a day.
- Only the consumption register (OBIS `1.8.0`) is used. On a bidirectional meter the
  feed-in register is ignored rather than added to consumption.
- Electricity only. Read-only — the integration never submits readings to the portal.

## Requirements

- Home Assistant 2025.2 or newer, with the recorder enabled (it is by default)
- A login for the Stadtwerke Elbtal customer portal

## Installation

### HACS (recommended)

1. In HACS, open the menu (⋮) at the top right and choose **Custom repositories**.
2. Enter `https://github.com/gus484/ha-stadtwerke-elbtal`, pick the type
   **Integration**, and add it.
3. Search for **Stadtwerke Elbtal** in HACS and download it.
4. Restart Home Assistant.

### Manual

Copy `custom_components/stadtwerke_elbtal/` from this repository into the
`custom_components/` folder of your Home Assistant configuration directory, then
restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → Stadtwerke Elbtal**

Enter the email address and password you use on the portal. They are checked by an
actual login before anything is saved. A meter can only be added once.

### Energy Dashboard

**Settings → Dashboards → Energy → Electricity grid → Add consumption**, then pick the
statistic **"Strom &lt;meter number&gt;"**. Do **not** add the meter-reading sensor as
well: it only knows readings from the day it was set up, and adding both counts the
consumption twice.

### Options

**Settings → Devices & services → Stadtwerke Elbtal → Configure**

| Option | Default | Range |
|---|---|---|
| Current reading, every … hours | 12 | 1–168 |
| Reading history, every … hours | 24 | 24–168 |

Saving reloads the integration. The portal updates readings monthly at best, so
shorter intervals do not bring newer data. They do bring more logins, since the portal
session usually expires between polls — and repeated failed logins can lock your
portal account.

### Upgrading from ITC PowerCommerce (0.1)

The domain changed from `itc_powercommerce` to `stadtwerke_elbtal`, so Home
Assistant treats 0.2 as a new integration. The old one stopped working with the
portal move anyway.

1. Remove the **ITC PowerCommerce** entry under **Settings → Devices & services**, and
   delete `custom_components/itc_powercommerce/` (HACS: remove the old download).
   The repository was renamed as well: in HACS, replace the custom repository
   `https://github.com/gus484/ha-itc-powercommerce` with
   `https://github.com/gus484/ha-stadtwerke-elbtal`. The old URL still redirects.
2. Install 0.2 and set it up as described above. The first history import fills the new
   statistic with every reading the portal holds — further back than the old portal.
3. In the Energy Dashboard, replace the old statistic with the new
   **"Strom &lt;meter number&gt;"**. Keeping both counts the consumption twice.
4. Optionally delete the old statistic `itc_powercommerce:swet_<meter number>_energy`
   under **Developer tools → Statistics**.

### Re-authentication

If the portal rejects the stored credentials, for example after a password change,
Home Assistant shows a re-authentication prompt. Check that you can still log in on
the portal's website before retrying, so failed attempts do not add up to a lock.

## Known limitations

- The portal only provides **monthly** values. In the Energy Dashboard these render as
  one bar per month, not as a smooth curve. That is expected, not a bug. Day and week
  views show nothing useful.
- The **first imported month shows 0 kWh**. There is no earlier reading to take a
  difference against.
- **Consumption lands in the month of the reading that closes it.** Home Assistant
  only sees the difference between two readings and books it on the date of the
  later one; it cannot know how that consumption was spread over the time in between.
  Most portal readings fall on month ends, which gives one correct bar per month.
  Where they do not, bars shift:
  - Missing months merge into the next reading. With no reading for October and
    November, the December bar holds all three months.
  - A reading in the middle of a month splits consumption at that date. With a
    reading on 14 May and the next on 20 June, May only covers the time up to the
    14th, and June carries everything from 14 May to its own month end, so June
    looks too high and May too low.
  - Two readings in one month (for example a self-reported one plus the regular one)
    simply add up in that month.
- The portal addresses its server-side functions by internal ids. If Stadtwerke
  Elbtal redeploys the portal, those can change; the integration then logs a parse
  error mentioning "The Apex request is invalid" until it is updated.
- Electricity only. Gas, water and district heating meters on the same account are
  ignored.

## Privacy

- Home Assistant stores config entry credentials in `.storage` in **plain text**. Anyone
  with access to your configuration directory or an unencrypted backup can read your
  portal password.
- **Diagnostics** (integration menu → *Download diagnostics*) are meant for bug
  reports. Username, password, meter number, contract and market location ids, name,
  address and email are redacted, and the statistic id is left out because it
  contains the meter number. Look through the file before you post it anyway.

## Troubleshooting

Enable debug logging in `configuration.yaml` and restart:

```yaml
logger:
  default: warning
  logs:
    custom_components.stadtwerke_elbtal: debug
```

If the portal changes, the import fails with a parse error in the log. A failing
history import leaves the current reading working. Please open an issue with the log
lines and a diagnostics file.

## Development

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest
```

The parser tests run against anonymized fixtures in `tests/fixtures/` and are the
early-warning system for portal changes. CI runs `hassfest`, the HACS
validation and `pytest` on every push and pull request, and weekly.

## License

MIT — see [LICENSE](LICENSE).
