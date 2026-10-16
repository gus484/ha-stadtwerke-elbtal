# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

Phase 0 (recon), Phase 1 (standalone Python prototype) and Phase 2 (the Home Assistant
HACS integration) are done. On 2026-10-16 the integration was ported to the
relaunched portal and renamed from `itc_powercommerce` to `stadtwerke_elbtal`
(version 0.2.0).

- `custom_components/stadtwerke_elbtal/api/` — the async portal client and CLI.
  Tests resolve it through `pythonpath` in `pyproject.toml`; there is no installable
  package.
- `logs/` — gitignored captures. `logs/2026-10-16/` holds the HARs of the relaunched
  portal (login to logout, and an expired session) and is the source of the
  anonymized fixtures in `tests/fixtures/`. The older files describe the ITC portal
  that no longer exists.
- `PLAN.md` — the original German work plan. **Local only, gitignored, not part of
  the repo.** It describes the ITC portal before recon and is wrong on almost every
  detail. Where it and this file disagree, this file wins.

Goal: read monthly electricity meter readings from the Stadtwerke Elbtal customer
portal into Home Assistant.

## What the portal looks like (since October 2026)

Until October 2026 the portal ran on ITC PowerCommerce (`/powercommerce/swet/fo/portal/…`,
`meterWidget.json`, `meterDetails` HTML). **All of that is gone.** The portal is now a
**Salesforce Experience Cloud site (LWR)** on the same host,
`https://onlineservice.stadtwerke-elbtal.de`. No captcha, no 2FA, no cookie banner.
The portal is specific to this utility — the Apex class ids are org-specific — so
there is no host/tenant to make configurable any more.

- **Every data call is an Apex call**: `POST /webruntime/api/apex/execute?language=de&asGuest=<bool>&htmlEncode=false`
  with JSON `{"namespace": "", "classname": "@udd/<class id>", "method": …,
  "isContinuation": false, "params": {…}, "cacheable": false}`. Success:
  `{"returnValue": …}`. Failure: HTTP 400 with `{"error": [{"message": …}]}`.
- **Login**: `GET /login` (sets consent cookies), then the Apex call
  `CustomerPortalLoginController.login` as guest with `username`, `password`,
  `startUrl: "/"`. It returns a `/vforcesite/secur/frontdoor.jsp?…&sid=…` URL;
  `GET` it to receive the session cookies. Then `GET /` — its HTML defines
  `LWR.define('@app/user', …)` with `isGuest` and **`csrfToken`**. Every
  authenticated Apex call sends it as the `CSRF-Token` header.
- **Contracts**: `OrderAndContractOverviewController.loadOrderAndContractOverviewFiltered`
  with `types: ["ServicePointContract"]`, paged (`numberOfItems`, `offset`,
  `totalItems`). `relatedObjectId` is the contract id, `medium` is `Electricity`.
  In the browser the user picks a contract after login; the client does not need that
  step, it addresses the contract by id.
- **Readings**: `PortalMeterReadingController.callGetMeterReadingsWithParams` with
  `objectApiName: "ServiceContract"`, `recordId: <contract id>`,
  `params: {offset, numberOfItems}`. Returns `meters[]` with `serialNumber` (the meter
  number, same as on the old portal), `totalNumberOfReadings` (50 in the capture,
  back to 2022), and `readings[]` newest first: `readingDate` (ISO date),
  `readingValue` (JSON number), `readingUnit` (`KWH`), `obis` (`1-1:1.8.0`), `type`
  (`Ablesung durch EVU`), `reason`, `status`.
- **Session liveness**: `GET /sfsites/c/_nc_external/system/security/session/SessionTimeServlet`
  answers `while(1);{"sp":…,"sr":<seconds left>}`; `sr` is `-1` once the session is
  gone. The portal's own frontend logs out on `sr <= 0`. Sessions last about 2 hours.
- **Apex class ids are the fragile part.** They change if the utility redeploys its
  controllers. Each id is registered in the page's view bundles as
  `LWR.define("@salesforce/apex/<Controller>.<method>", …getApexInvoker("", "@udd/<id>", "<method>", …)`,
  so a changed id can be looked up there (the login view, `home_view`,
  `service_Contract_Detail_view`). The ids are constants in `api/client.py`, commented
  with the controller name. Discovering them at runtime is a candidate refinement,
  not implemented. A call naming an unknown class answers
  `"The Apex request is invalid."` → `ParseError`.

## Conventions

- **Use `Decimal`, never `float`, for meter readings** throughout the client and
  models. The portal sends readings as JSON numbers, so parse with
  `json.loads(…, parse_float=Decimal)` (`api.parsing.loads`) — a float detour turns
  `13114.7` into `13114.699999…` before anything else sees it. Convert to `float`
  only at the boundary where an external API demands it (see Statistics below).
- **Parse the known field names directly.** No generic scraping.
- **Session expiry is detected by asking, not by guessing.** The old portal had two
  silent expiry shapes, and a guessed markup marker produced a 13-day blind spot in
  2026-09. The new client:
  - asks `SessionTimeServlet` before each coordinator run and logs in when `sr <= 0`.
    With 12 h/24 h polls and ~2 h sessions this means a login on nearly every run —
    expiry is the normal case.
  - treats an authenticated Apex call answered with 401/403 or non-JSON as
    `SessionExpired`, re-logs in once and retries. The dead-session answer of the
    Apex endpoint has **not been captured** yet (the expired-session HAR shows only
    the frontend's servlet check and logout), so this branch is defensive.
  - `SessionExpired` that survives the retry is a transient `PortalError` →
    `UpdateFailed`, **never `AuthError`**: the fresh login just succeeded.
  - `AuthError` comes only from the login call itself. A login *error* with
    "The Apex request is invalid." is `ParseError`, not `AuthError`, and an accepted
    login after which `/` still shows a guest is `PortalError`. Anything else would
    drive a working account into the reauth dialog and repeated logins, which is how
    portals lock accounts.
  - The response to a **rejected** login was not captured (deliberately — no failed
    attempts against the account). The tests use the captured Apex error envelope with
    a stand-in message.
- **Do not hammer the portal.** Repeated failed logins can lock the account. During
  development, work against the fixtures.
- **The parser test is the priority test.** It is the early-warning system for portal
  changes.
- Data scope: electricity only. Don't code against gas/water/heat, but don't hardcode
  against them either. Only the consumption register (OBIS `…:1.8.0`, compared after
  the colon as a whole — `21.8.0` is phase L1) feeds sensor and statistics; feed-in
  `2.8.0` on a bidirectional meter would otherwise be summed into consumption.

## Fixtures / privacy

Before anything from `logs/` is committed as a fixture, redact: password, cookies,
CSRF token, session ids (`sid=` in the frontdoor URL), Salesforce record ids (user
`005…`, contact `003…`, contract `810…`, org `00D…`), meter serial and device id,
market location (MaLo — also inside `robotronUrl`), contract and account numbers,
name, address, email, phone, IBAN. Use consistent placeholders so fixtures stay
parseable: contract `810000000000001AAA`, meter `1ABC0000000001`, MaLo `50000000000`.
Grep the fixtures for the real values afterwards.

Diagnostics in the integration must use `async_redact_data` over `REDACT_KEYS` in
`const.py`.

---

# Home Assistant integration

## Repo layout

HACS expects the integration at `custom_components/stadtwerke_elbtal/` in the repo
root. Domain is `stadtwerke_elbtal`. (It was `itc_powercommerce` until 0.1.0; the
platform name stopped fitting when the utility left ITC.) The GitHub repository is
still `gus484/ha-itc-powercommerce`.

The client is vendored into the integration directory rather than published to PyPI.
Keep the module boundary clean (client knows nothing about Home Assistant) so
extraction to PyPI stays possible later.

```
custom_components/stadtwerke_elbtal/
  __init__.py        async_setup_entry / async_unload_entry
  manifest.json
  const.py
  api/               vendored client — no HA imports below this line
  coordinator.py
  config_flow.py
  sensor.py
  series.py          statistic row logic, no HA imports
  statistics.py
  diagnostics.py
  strings.json
  translations/de.json, en.json
hacs.json
```

### manifest.json

- `"config_flow": true`, `"iot_class": "cloud_polling"`
- `"version"` must match the Git tag exactly or HACS rejects the release
- `"requirements": []` — the client needs only aiohttp, which Home Assistant ships.
  BeautifulSoup went with the HTML parser.
- **Do not pin aiohttp.** The `aiohttp<3.12` pin in `requirements.txt` is a dev-only
  workaround for `aioresponses`. Carrying it into `manifest.json` would fight Home
  Assistant's own aiohttp and break the install.

## The client inside Home Assistant

- Use `async_create_clientsession(hass)`, **not** `async_get_clientsession(hass)`.
  The portal authenticates with session cookies, and the shared session's cookie jar
  would keep them across a config entry reload and hand them to the next client.
  Created in `async_setup_entry`, HA detaches the session on unload. The config flow
  uses a throwaway one (`auto_cleanup=False` plus `detach()`).
- `login()` clears the portal's cookies before it starts, so every login is a fresh one.
- Both coordinators share one client; an `asyncio.Lock` serializes logins so they
  cannot clear each other's cookies.
- Keep all blocking work out of the event loop; the client is already async.

## Coordinators — two rhythms, not one

- **Meter coordinator**, every 12 h: contracts, then one short readings page
  (`numberOfItems: 4`) per electricity contract → current reading per meter.
  More than one item because a bidirectional meter may list feed-in first.
- **History coordinator**, at setup then every 24 h: pages through all readings per
  meter (page size 18, as the portal frontend uses) and runs the statistics import.

Error mapping: `AuthError` → `ConfigEntryAuthFailed` (triggers the reauth dialog),
everything transient → `UpdateFailed`.

## Statistics — the part most likely to go wrong

Historical values are written via
`homeassistant.components.recorder.statistics.async_add_external_statistics`. A normal
sensor cannot backfill the past.

- `statistic_id` needs the domain prefix with a colon:
  `stadtwerke_elbtal:<meterno>_energy`
- `StatisticMetaData` with `has_sum=True`, `unit_of_measurement="kWh"`,
  `source="stadtwerke_elbtal"`
- Readings are `Decimal`; the statistics API takes `float`. Convert immediately
  before writing, nowhere earlier.
- `readingDate` is a bare ISO date. Build midnight in `Europe/Berlin`, then convert to
  UTC. (`readingDateTime` is a UTC timestamp of the portal's record, not of the
  reading — do not use it as `start`.) **`start` must be hour-aligned and
  timezone-aware** or the recorder rejects the row.
- `state` is the meter reading; `sum` is the meter reading **minus a baseline**, so
  the oldest row has `sum = 0`. HA derives consumption from the delta between
  consecutive `sum` values, and for the oldest row it computes `sum − 0` — a raw
  meter reading as `sum` puts the entire reading into the first month.
  `sum` must also never decrease. There is no `last_reset` for external statistics.
- The baseline is read back from the recorder (`state − sum` of the oldest stored
  row), falling back to the earliest reading when nothing is stored. It is not kept
  in the config entry. That keeps re-imports consistent after the portal's window
  has moved past the first import.
- Imports are idempotent on `(statistic_id, start)`, so the full history can be
  rewritten on every backfill without diffing.
- The row logic lives in `series.py` with no HA imports, so `tests/test_series.py`
  covers it without installing Home Assistant.
- A `DataUpdateCoordinator` only reschedules while it has listeners. The history
  coordinator has no entities, so `__init__.py` gives it a no-op listener — without
  it the backfill runs once at setup and never again.

**Known limitation, documented in the README:** monthly values render in the Energy
Dashboard as one bar per month, not as a smooth curve. Linear distribution across the
month's hours looks better but invents data — if implemented at all, make it an
option that is off by default.

**Test the statistics import against a throwaway HA instance, not a production one.**

## Config flow

- One step: username / password, validated by a real login plus contract and meter
  lookup. The utility selection and the manual host/tenant step went with the ITC
  platform.
- `async_set_unique_id(meter serial number)` followed by
  `abort_if_unique_id_configured()`, or the same installation can be added twice.
- Reauth flow handling `ConfigEntryAuthFailed`.
- Options flow for the two poll intervals.
- `strings.json` plus `translations/de.json` and `translations/en.json`. Error keys for
  `invalid_auth`, `cannot_connect`, `no_meter`, `unknown`.

## Entities

- `_attr_has_entity_name = True`
- `DeviceInfo` identified by meter number, so sensors group under one device
- `unique_id` derived from the meter number, not from the config entry ID
- Current-reading sensor: `device_class: energy`, `state_class: total_increasing`,
  `native_unit_of_measurement: kWh`. Attributes: reading date, meter number, reading
  source (`read` / `estimated` / `self_reported`) mapped from the portal's `type`.

## CI

GitHub Actions running `hassfest`, the HACS validation action and `pytest`. These
catch manifest and structure errors that otherwise surface only at the user's install.

## Out of scope

- Writing meter readings back to the portal (`PortalMeterReadingPostController`
  exists — do not call it). Read-only.
- Gas, water, district heating.
- Submission to the HACS default store (needs a `home-assistant/brands` PR for icon
  and logo). Custom-repository install is the target for now.
