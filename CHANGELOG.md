# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `manifest.json`'s `codeowners` is `@mmnlfrrr`, this fork's maintainer,
  rather than upstream's author. Home Assistant shows that name as the
  person responsible for the integration's code, and it decides who a
  core-side mention reaches - which should not be someone who has never
  seen any of the code added here. Upstream's authorship of everything up
  to 0.3.3 is credited in the README instead, where it belongs.
- **A poll is five requests now, whatever the room count.** It was seven
  plus one per room - fourteen on a seven-room installation - because
  boost was read one room at a time, and the decision settings, Breeze
  and per-room CO2 demand each had their own endpoint.

  All four are slices of `/v2/decision`, which answers with the lot in a
  single response: the same device-wide block `/v1/decision` returns, the
  same objects `/v2/decision/breeze` and `/v2/decision/room` return, and
  each room's `boost` exactly as `/v1/api/boost/{id}` gives it. Confirmed
  against a real capture, which is now a fixture.

  The four parts are still parsed separately, so the merged read degrades
  the way the four separate ones did: one room's malformed boost block
  costs that room's boost entity, a missing `breeze` costs Breeze, and
  neither takes demand control, the silent schedule or the minimum
  ventilation level down with it. Without an API key - and if that single
  read fails - boost still comes from the per-room endpoint, which is the
  one control confirmed to work without a key.

  The sub-resources are still used for writes, which are per-setting.
- **A poll no longer walks its endpoints one at a time.** `data/current`
  still goes first alone - it decides whether this is a v1 or a v2 poll and
  which rooms exist - but the reads behind it now go out together, with the
  client capping how many are actually in flight rather than landing the
  lot on a small embedded unit at once. Sequentially, at up to the 10s
  per-request timeout, a slow poll could outlast the interval that
  scheduled it. (This is what first cut the poll's wall time; the entry
  above later cut the number of requests itself.)

  `/renson_core/v2/global` is also re-read every tenth minute rather than
  every poll: firmware version, MAC and IP change on a firmware update or a
  network move, not between two polls. Only successful reads are reused - a
  failure still takes the entities built on it unavailable and asks again
  next poll, rather than serving a stale reading for ten minutes.
- **Diagnostics now dump everything the coordinator holds**, not a
  selection of it: decision, Breeze, per-room decisions, global info,
  device errors, `/v1/device` telemetry and Wi-Fi status have been added to
  the existing room and boost data. A bug report about a wrong reading is
  usually a bug report about one of the reverse-engineered endpoints, and
  those were exactly the ones missing.
- `sensor.py` is split into `sensor_room.py` and `sensor_unit.py` behind a
  76-line platform entry point. The two halves differ in kind, not just in
  size: a room sensor exists only if that room reports the reading behind
  it and must be buildable again for a room that appears later, while a
  unit sensor is created once from a fixed list.
- **Each ventilated room is now its own device**, linked back to the unit
  by `via_device_id`, instead of every entity sitting on one device. The unit
  device keeps everything describing the appliance as a whole (air quality,
  ventilation level, power, fan readings, `Boost all`, demand control,
  Silent, diagnostics); room-scoped entities move to their room's device.

  The reason is areas. Home Assistant assigns areas per device, so with a
  single device the only way to get one room's sensors into that room's
  area was to move them one at a time - and entity IDs generated before
  such a move keep whatever prefix they were given, which is how an
  install ends up with IDs like
  `sensor.bathroom_healthbox_kitchen_duct_conductance`. One device per room
  makes it one assignment per room, with every sensor following.

  Room entity names lose their room prefix as a result (`Toilet
  Temperature` becomes `Temperature` under a `Toilet` device): the device
  already says which room, so repeating it was redundant.

  **Existing installs keep their entity IDs and history** - unique IDs are
  unchanged, and Home Assistant only derives an entity ID once, when the
  entity is first created. The visible changes are the device grouping and
  the shorter names. Only a fresh install (or a removed-and-re-added
  integration) gets the shorter entity IDs.
- Ventilation profile icons now match the ones Renson's own app uses: a
  plain heart for `health` (was a heart-with-pulse-line) and a tornado for
  `intense` (was the generic wind glyph). The wind glyph is also the
  default icon of three airflow/ventilation sensors, so `intense` used to
  be visually indistinguishable from them in a list. `eco` keeps its leaf,
  which already matched.
- **Ventilation profile options are now translated.** The profile select
  offered the device's own `eco`/`health`/`intense` with no labels, so they
  showed raw - where Renson's own app says Eco/Health/Intense. Now
  translated in all three languages, with a test deriving the expected set
  from the code so a new profile cannot ship unlabelled.
- Several French labels re-checked against the wording of Renson's French
  app: firmware version, exhaust pressure, total extraction airflow, and the
  air quality scale (`Excellent`/`Moyen` rather than `Très bon`/`Modéré`).
  `Exhaust pressure` was also a mistranslation of this integration's own
  English, independently of Renson.
- **The air quality bands now carry Renson's own names in every language,
  everywhere they appear.** English reads Excellent/Good/Moderate/Bad/Very
  bad, matching the four labels in Renson's app plus a fifth for values
  past 100; Dutch already used Renson's own Dutch words and is unchanged.
  The re-wording above had reached only the `AQI level` sensors, leaving
  the identical band named `Modéré` in the `qualification` attribute of
  the sensor feeding them and `Moyen` on the tile - one band, two words. A
  test now holds all four naming sites to a single vocabulary.

  The boundaries themselves are untouched, and remain Renson's: the app
  reads its label as a string from the cloud rather than deriving it from
  a number, so it has no thresholds of its own to adopt. Band *states* are
  unchanged keys, so history and automations are unaffected.

### Added

- **Diagnostics per device, not only per integration.** The Download
  diagnostics button on a room's device page now answers with that room
  only: its parsed state, boost status, staged boost level and duration,
  its boost ceiling and its CO2 demand settings. The unit's own page
  answers with what describes the appliance as a whole - decision
  settings, Breeze, telemetry, Wi-Fi, errors, identity - without the
  per-room noise.

  With a device per room, "this room reads wrong" is the shape most bug
  reports take, and the config entry's dump answered it with every other
  room attached. That dump is unchanged and still one click away, on the
  integration rather than the device. A device left behind by a vent the
  unit no longer reports says exactly that, which is usually the bug being
  reported.
- **A system health page** (**Settings** → **System** → **Repairs** → ⋮ →
  **System information**), answering the first round of every support
  exchange without anyone having to produce a diagnostics file: whether
  the device answers at all, its firmware version, whether privileged (v2)
  access is active, how often it is being polled, whether the last poll
  worked, and how many rooms came back.

  It carries nothing identifying - no IP, no serial, no MAC, not even
  indirectly through the config entry's title, which is
  `Healthbox 3 (<serial>)`. Diagnostics is a file the user deliberately
  attaches; this page sits in Settings and gets screenshotted into forum
  threads, so with more than one Healthbox its rows are numbered rather
  than named.
- **The poll interval is configurable** (**Settings** → **Devices &
  Services** → **Renson Healthbox 3** → **Configure**), between 15 seconds
  and 10 minutes. The entry reloads itself when you change it. See
  [Data updates](README.md#data-updates) for what one poll actually costs
  before shortening it.
- **A vent added in Renson's own app now appears without a reload.** Rooms
  were read once, at setup; a new one stayed invisible until somebody
  thought to reload the integration, with nothing anywhere saying that was
  what it needed. Entities for a room id not seen before are now created on
  the poll that first reports it.
- **The device of a room that no longer exists can be deleted.** Home
  Assistant refuses every device deletion unless an integration says
  otherwise, so a physically removed vent used to leave a device behind for
  good, its entities stuck on unavailable and its Delete button inert. The
  unit and any still-reported room are still refused - deleting one of
  those would only have it recreated on the next poll.
- The three workflows that had no manual trigger (CI, mypy, hassfest) can
  now be run from the Actions tab. A push made with an app installation
  token deliberately does not start a workflow run, which is why this
  repository's own history shows none.
- A test tying the per-language translation checks to the files actually
  on disk, so a language cannot be added - or dropped - without the checks
  following it.
- **A Healthbox card**, drawing the unit and its outlets the way the
  Renson app does: a numbered connection at each wired collector port and
  a cap everywhere else, with the unit centred. Hovering an outlet shows
  its room - pictogram, name, the Home Assistant area it sits in, its
  regulatory code, airflow, air quality, profile and boost - with that
  room's airflow percentage on the panel's own title line, beside the
  outlet number, since it is the figure the panel is opened for. Clicking
  it opens that room's more-info, and the card's header carries the
  whole-house ventilation level beside the unit's name. The hover panel is placed with
  CSS anchor positioning where the browser has it, so it flips itself to
  stay inside the card near an edge, and falls back to a fixed offset
  where it does not. Add it from the card picker;
  the integration serves it, so there is no resource to register by hand.

  Nothing about it is per-install: the integration publishes the real
  topology (which ports carry a room, their names, their entity ids) at
  `/api/healthbox3/layout`, so the same card works on any unit and in any
  language. The artwork and the 540x540 stage geometry come from the app's
  own drawables and decoded layout, not from a redrawing.

  A physical outlet split into several branches gets Renson's dotted
  numbering (`1.1`, `1.2`, `1.3`), drawn as a chain running away from the
  unit exactly as the Renson installer app draws it, each branch with its
  own connection and its own fault state - on any edge, with the drawing
  sizing itself to whatever the chains need. An outlet is marked faulty only
  when a reported error's association id is exactly one of this unit's
  port numbers - `/v1/error` says nothing about what that id identifies,
  so anything else is shown against the unit rather than blamed on a
  room.
- **Each room is now illustrated with Renson's own pictogram.** The
  `Room symbol` sensor carries the drawing Renson's app uses for that
  room - bathtub, chef's hat, toilet, bed - with no per-install setup. The
  icons ship as a `healthbox:*` icon set the integration serves to the
  frontend.

  21 of Renson's 23 zone pictograms are shipped as-is. Two cannot be:
  `cooker_hood` is line art, which Home Assistant's icon renderer would
  fill into a blob, and `studio` is two paths under different fill rules
  that cannot be combined into the single path the renderer takes. Both
  were rasterised and compared rather than assumed; they differ from the
  original by 27% and 17% of the canvas. Their symbols fall back to a
  neighbouring icon (kitchen and bed), and anything outside the known
  vocabulary falls back to a generic house, as Renson's own picker does.
- Per-state icons for the two `AQI level` sensors. They are five-band enum
  sensors that had no entry in `icons.json` at all, so Home Assistant drew
  the same generic icon for "Excellent" and for "Very bad". They now use
  the same mechanism the profile select and boost fan already did: a
  default matching their numeric twins, and a scale of faces across the
  five bands.
- `IP address`, `MAC address` and `Connection type` sensors (diagnostic),
  from the `/renson_core/v2/global` response that was previously read only
  for the firmware version. `Connection type` is the one that fills a real
  gap: `Wi-Fi status` answers "not connected" on a unit wired over
  Ethernet, which reads like a fault until you know it is on a cable.

  The unit's device entry also gains the MAC as a network connection -
  what lets Home Assistant recognise the unit after an address change -
  and a configuration URL pointing at the unit's own web interface. Both
  are omitted rather than guessed when the endpoint is unreachable.
- `Room symbol` sensor per room (diagnostic): which pictogram the device
  itself picked for the room. Lets a dashboard illustrate each room without
  the layout being written by hand per install - Home Assistant has no
  per-device icon, so a card is the only place a picture can appear, and
  this gives the card something to template off.

  Taken from the device's `icon` parameter, falling back to the room's
  `type` when it's blank. The two are different fields and can disagree: a
  room typed `BedRoom` can carry the `StudioFlat` icon, which Renson's own
  UIs draw - so a card templating off the type would be wrong on exactly
  those rooms. Reported in Renson's spelling, not translated into `mdi:`
  names, which would be a guess for every value beyond the handful seen.
- `Legislation code` sensor per room (diagnostic): the room's regulatory
  destination code (`C16`, `C22`, `C3`…), which is what the ventilation
  standard classes the room as and therefore what set its nominal flow at
  commissioning - the context that explains why one room's Qnom is 45 m³/h
  and another's 26. Shown beside each room's name in the Renson app.
  Reported by some units and not others, so it's created per room only
  where the device actually reports it, and a blank value counts as
  absent.
- `Valve port` sensor per room (diagnostic): which collector port the
  room's valve is wired to - the number printed on the unit, and the one
  the Renson app lists rooms by. Already used internally to join rooms to
  the duct model, now exposed so a schematic or floor-plan dashboard can
  place each room against the right outlet instead of hardcoding the
  layout per install. Unlike `Valve pressure` and `Duct conductance`, it
  comes from `data/current` rather than the duct model, so it stays
  readable on an uncalibrated unit.
- `Energy` sensor: cumulative kWh, usable in the Energy dashboard as-is,
  with no Riemann-sum helper to wire up by hand. Integrated from the
  whole-device `Power` reading, trapezoidally between polls, with the
  running total restored across restarts. Gaps longer than 15 minutes are
  skipped rather than extrapolated - past that there is no telling a Home
  Assistant outage from the unit being off, and under-reporting beats
  inventing energy.
- Electrical power sensors (`Power` for the whole device, `Fan power` for
  the fan alone). Both are real, distinct device readings - the fan-only
  figure is the lower of the two, so the whole-device one is what belongs
  in the Energy dashboard. See
  [Energy dashboard](README.md#energy-dashboard) for the one Riemann-sum
  helper that turns it into kWh.
- Fan telemetry sensors: `Fan airflow` (m³/h), `Fan speed` (rpm), plus
  `Fan voltage` and `Fan pressure` as diagnostics.
- Duct network sensors: `Duct network pressure`, `Exhaust pressure`,
  `Outlet conductance` and `Network leakage`, plus per-room
  `<room> Valve pressure` and `<room> Duct conductance`. Together these
  expose the physical duct model (`Q = C x sqrt(dP)`) the device
  calibrates for itself; a sustained drop in a room's conductance is an
  early sign of a duct or valve fouling up. See
  [Duct model](README.md#duct-model).
- `<room> Airflow rate` and `<room> Nominal airflow`, both in m³/h. The
  existing `<room> Airflow` reports a percentage of nominal, which can't
  be summed or compared as a real extracted volume.
- New `binary_sensor` platform: `Problem` (the boolean companion to the
  existing `Device errors` count, for automations), `Internet connection`,
  and `Advanced API access` - the last one is created even without a
  working API key, since reporting that privileged access is *off* is the
  whole point of it. Losing that access is otherwise silent, and its
  symptom (every per-room air-quality entity going unavailable while the
  device looks healthy) is easy to misread.
- `Wi-Fi status` diagnostic sensor, carrying the SSID as an attribute.

All of the above read `/v1/device` and
`/renson_core/v1/wifi/client/status`, neither of which was previously
called. Like every other reverse-engineered endpoint here, both are gated
on an active API key.

### Fixed

- **The VOC sensor now declares a device class**
  (`volatile_organic_compounds_parts`, Home Assistant's own class for a
  VOC *ratio* in ppm/ppb, as opposed to a density in µg/m³). It had none,
  which cost it its proper icon and - the visible part - the per-entity
  **Unit of measurement** picker every other physical reading here has.
  Its unit, its state and its history are unchanged.
- **A room whose boost level exceeds what Renson's app offers is no longer
  clamped to it.** The boost scale was fixed at 10-200%, described as "the
  range offered by Renson's own app" - but the device does not stop there.
  A real unit stores `default_level: 270` for its kitchen: the French
  hygro B peak extraction rate, 135 m³/h of a 50 m³/h nominal, written in
  at commissioning. Home Assistant rounded that down to 200 and asked the
  kitchen for 100 m³/h - a regulatory figure quietly rewritten, with every
  entity still looking perfectly plausible.

  Each room's ceiling is now the higher of 200% and the level the device
  itself reports as that room's default, refreshed every poll so a room
  re-commissioned at the unit follows. **Rooms inside the app's range keep
  exactly the scale they had**, which is why this is per room rather than
  one wider global range: a percentage already written into an automation
  for any other room still means what it meant. `Boost all` keeps the
  200% range too - it sends one level to every room at once, and a level
  above the app's range is only known to be accepted by the one room that
  stores it.

  A new `level_max` attribute says what 100% on a given fan actually asks
  for, since that is no longer the same figure on every room.
- **Diagnostics published the device's MAC address and IP in clear.**
  `async_redact_data` matches keys exactly, and this device's address
  arrives under two spellings - `MAC`/`IP` from the discovery payload,
  `mac`/`ip` from `/renson_core/v2/global`. Only the uppercase pair was
  listed. A diagnostics file is what gets pasted into a public issue.
- **An unparseable silent-schedule time took both time entities out
  entirely.** `datetime.time.fromisoformat` raises on anything that is not
  `HH:MM:SS`, and an exception raised from `native_value` does not degrade
  gracefully: Home Assistant fails to add the entity at all, leaving no
  silent-schedule control anywhere in the UI and a traceback in the log.
  Now reads as unknown, which is the honest state for a value the device
  sent that this integration cannot make sense of.
- **Each startup logged three deprecation warnings naming this
  integration.** Rooms were linked to the unit with `via_device`, which
  Home Assistant has replaced with `via_device_id`; it still worked, but
  wrote "Detected that custom integration 'healthbox3' ... will stop
  working in Home Assistant 2027.8.0" into the user's log on every setup,
  with a link to this project's issue tracker. It would have stopped
  working outright in 2027.8.

  The link is not a rename: `via_device_id` wants the unit's registry id,
  which only exists once its device does. The unit's device entry is now
  created explicitly during setup, before any platform is forwarded, so
  the id is guaranteed to be there rather than inferred from the order the
  platforms happen to be listed in. Existing installs are unaffected - the
  id is the same one the registry had already resolved.

  Two tests came with it: one asserting every room actually nests under the
  unit, which nothing checked before, and one asserting setup logs no
  deprecation report at all - `via_device` produced three on every startup
  and the suite stayed green throughout.
- **The room pictograms never appeared.** Every icon was asked for as
  `custom:renson-bath`, which asks the frontend for a set called `custom`
  (that is the prefix for Lovelace *cards*, not icons) and an icon called
  `renson-bath`, and gets neither. Nothing reports this: an unresolvable
  icon renders as empty space, so it read as a styling problem. They are
  now `healthbox:bath`, built in one place, and a test holds them to the
  set actually registered.

  The set is named after the product rather than the manufacturer on
  purpose: `window.customIconsets` is one namespace shared by every
  integration in the frontend, first registration wins, and Home Assistant
  core already ships an integration whose domain is `renson`.
- **The hover panel could hang outside the card** on browsers without CSS
  anchor positioning - which today means Firefox and Safari, not an exotic
  corner. The fallback still cannot flip the way anchor positioning does,
  but it now measures itself and stays inside, and drops below an outlet
  when there is no room above it. Its pictogram box is also sized up front
  rather than left to size itself when the icon resolves, which was making
  the panel jump wider a frame after it appeared.
- **A correct API key was reported as rejected.** Activating a key is
  asynchronous on the device's side - it has to reach Renson's servers to
  check the key against its own serial, and answers `validating` until
  that finishes. The config flow read the status once, immediately after
  sending the key, landing squarely inside that window, saw something
  other than `valid` and announced a rejected key. The same key then
  worked.

  All three places a key can be entered (setup, reconfigure, reauth) now
  wait for the device to actually answer, and a device that never decides
  gets its own message - it needs internet access - instead of the key
  taking the blame. The same state at startup no longer triggers a reauth
  prompt or a silent fall back to v1 either: a device revalidating its own
  key, which is what it does after a reboot, is waited out.

## [0.3.3] - 2026-07-14

### Added

- New `AQI level` sensors (per-room and whole-house), showing the same
  qualification band (Very good/Good/Moderate/Poor/Very poor) already
  available as the numeric AQI sensors' `qualification` attribute, but as
  their own primary, dashboard-friendly state - the numeric sensors are
  unchanged, so anything already graphing AQI history keeps working. See
  [Known limitations](README.md#known-limitations) for the same
  comparability caveats that apply to the `qualification` attribute.

## [0.3.2] - 2026-07-13

### Added

- Air quality index sensors (per-room and whole-house) now include a
  `qualification` attribute (Very good/Good/Moderate/Poor/Very poor)
  alongside the existing raw numeric value, per Renson's own official
  guidance on how to interpret the index. See
  [Known limitations](README.md#known-limitations) for two comparability
  caveats Renson also raised: values aren't strictly comparable across
  rooms with different sensor types, and the whole-house value isn't
  necessarily equal to whichever room its attributes point at.

## [0.3.1] - 2026-07-10

### Added

- Device-reported errors now include a short `category` label (e.g.
  "Power", "Fan and main PCB") alongside the existing code and
  description, on both the repair issue and the `Device errors` sensor.
  Best-effort only - see [Known limitations](README.md#known-limitations),
  since it's sourced from Renson's public error-code index rather than
  the local API and hasn't been confirmed against a real device error.

## [0.3.0] - 2026-07-09

### ⚠️ Breaking changes

- **The demand control switch's on/off meaning is now correct but
  inverted from 0.2.0's buggy behavior.** If you have automations or
  dashboards referencing this switch, check anything depending on its
  state - this is the one that fails silently rather than obviously.
- **The per-room CO2 threshold number now represents a different value
  than before** (the device's `maximum` field, not `minimum`) - if you
  have automations setting or reading this number, its meaning and
  typical value have both changed.
- **The `Breeze` switch has been removed.** Breeze can no longer be
  turned on/off from Home Assistant, only its trigger temperature - use
  the Renson app or the device itself for on/off.

### Added

- Automatic reconnection after an IP change can now also be triggered by
  Home Assistant's own passive DHCP discovery, alongside the existing
  active network search - including for a device that's never connected
  successfully even once, which the active search alone couldn't help
  with.
- Diagnostic firmware version sensor - requires an active API key.
- Device-reported errors now create a Home Assistant repair issue
  (Settings > Repairs), plus a diagnostic sensor with the current error
  count and most recent error's details - requires an active API key.
  Clearing an error still requires the Renson app or the device itself;
  see [Known limitations](README.md#known-limitations).
- Dutch translation, reviewed by a native speaker. French translation,
  best-effort only and not yet reviewed by a native speaker - see
  [Translations](README.md#translations).

### Changed

- Switched the integration's icon and logo to Renson's own Healthbox 3
  branding, replacing the placeholder generic ventilation-fan icon used
  since 0.1.0.

### Removed

- The `Breeze` switch, added in 0.2.0. Neither Renson's mobile app nor its
  own device web UI ever exposes an on/off control for Breeze (only its
  trigger temperature, which the `Breeze temperature` number entity still
  controls) - the same restraint this integration already applies to
  `fire_protect` and Qmin/Qnom/Offset. Use the Renson app or the device
  itself to turn Breeze on or off.

### Fixed

- **The per-room CO2 threshold number was reading and writing the wrong
  field.** It showed and set `minimum`, but confirmed against a fresh
  device capture cross-referenced with the Renson app, the app displays
  and edits `maximum` - so the value shown/set in Home Assistant since
  0.2.0 didn't match what the app showed, and editing it moved the wrong
  end of the underlying range. Now reads/writes `maximum`, deriving
  `minimum` to preserve the same span as before.
- **The demand control switch was inverted.** It showed ON/OFF as the
  raw `program.enable` device field, but confirmed against a fresh
  device capture cross-referenced with the Renson app, `program.enable`
  tracks the clock/schedule fallback being active - the opposite of
  "demand control is active". The switch now presents and writes the
  negation of that field, so its state matches the Renson app.

## [0.2.0] - 2026-07-09

### Added

- Demand control switch, to toggle automatic (sensor-driven) ventilation
  on or off.
- Minimum ventilation level number, setting the device-wide floor
  ventilation percentage.
- Breeze switch and temperature number, for temperature-triggered night
  cooling.
- Per-room CO2 threshold number, only created for rooms that support
  CO2-triggered demand control.
- Silent schedule switch, reduction number, and start/stop time entities,
  for the reduced-noise schedule.
- Whole-house ventilation level sensor, showing the current aggregate
  ventilation level as a percentage.

  All six require an active API key, the same as the ventilation profile
  selector.

### Changed

- The error messages for "a room was removed from the device while an
  action was in progress" and "boost all partially failed" are now
  translatable into other languages if a translation is ever added
  (still English-only for now, since only an English translation
  exists). Both messages also gained a trailing period.
- Every sensor, the ventilation profile selector, and both boost fan
  entities now show a specific icon instead of a generic fallback -
  including the profile selector showing a different icon per profile
  (eco/health/intense), and both boost fans showing a distinct icon
  when off vs on.

## [0.1.4] - 2026-07-07

### Added

- Per-room airflow sensor, showing current airflow as a percentage of that
  room's rated (nominal) flow. Only created for rooms that report the
  underlying data. Not a 0-100 bounded percentage - boost can drive
  airflow well past nominal, so values can run from roughly 10% up to
  200%.
- Automatic, silent reconnection if a Healthbox 3's IP address changes
  after setup (e.g. a DHCP lease renewal) - no notification, no action
  needed. Only applies once an entry has connected successfully at least
  once; see README "Known limitations" for the one case it doesn't cover.

## [0.1.3] - 2026-07-07

### Added

- Automatic network discovery during setup: the config flow now tries a UDP
  broadcast to find your Healthbox 3 before asking for an IP address.
  Exactly one device found is offered for confirmation; multiple found are
  offered as a selection list; devices already configured are excluded
  from either. If nothing responds within a few seconds - broadcast
  delivery is unreliable on some networks (AP client isolation, IGMP
  snooping, VLAN segmentation) - setup falls through to manual IP entry
  with no error shown, exactly as before.
- After a manually-entered IP passes connectivity validation, setup makes
  a best-effort attempt to also show a confirmation screen with the
  device's MAC address and firmware version. If that probe doesn't
  respond, setup continues straight on exactly as before - the device was
  already verified reachable, so this is purely extra detail, never a
  requirement.

## [0.1.2] - 2026-07-07

### Changed

- Expanded the boost duration preset list from 5 options to 8: `5 min`,
  `10 min`, `15 min`, `30 min`, `45 min`, `1 hour`, `2 hours`, `4 hours`
  (previously `15 min`/`30 min`/`1 hour`/`2 hours`/`3 hours`). Still a
  fixed preset list, not an arbitrary custom duration - 5 minutes is
  also the shortest boost Renson's own app offers.

## [0.1.1] - 2026-07-06

### Changed

- No functional changes. Added hacs/action and hassfest validation
  workflows required for HACS default-store submission.

## [0.1.0] - 2026-07-06

### Added

- Config flow with manual IP entry, v1 connectivity validation, optional
  API key activation with verification, automatic detection of a key
  activated directly against the device (outside this integration), and
  a reauthentication flow.
- Reconfigure flow (Settings → Devices & Services → Reconfigure) for
  updating the device's IP address and/or API key in place, without
  losing entity history from a remove-and-re-add. Re-validates
  connectivity and confirms (by serial number) it's still the same
  device before updating anything; a blank API key field leaves the
  currently stored key untouched.
- `DataUpdateCoordinator` polling `/v1/api/data/current` or
  `/v2/api/data/current` depending on API key status, with automatic
  graceful fallback to v1-only functionality (rather than repeated
  errors) if a previously valid key stops working.
- Per-room sensors for temperature, humidity, CO2, VOC, and air quality
  index, created dynamically to match whatever sensors a given room's
  hardware actually reports, plus a whole-house air quality index sensor.
- Boost control as `fan` entities - one per room plus one device-level
  "Boost all" - with percentage and preset-mode duration mapped onto the
  device's real level/duration ranges, fully automatable via standard
  Home Assistant fan services.
- Ventilation profile selection per room (eco/health/intense), available
  once an API key is active.
- Diagnostics support, with serial numbers, warranty numbers, API keys,
  host/IP, and other identifying fields redacted.
- Local brand icons (a generic ventilation symbol, not Renson's actual
  branding).
- MIT license.
- Automated tests (config flow, coordinator, entities, diagnostics, and
  real Home Assistant automation-engine dispatch) and CI running them on
  every push and pull request.
- README "Use cases", "Examples", and "Troubleshooting" sections.

<!--
  This repository carries no tags yet, so a link naming one (0.3.3, say)
  would 404 here however it is spelled. The Unreleased link therefore
  compares from the commit upstream released as 0.3.3, by its SHA, which
  does exist in this history. Once a release is tagged here, this becomes
  a normal tag-to-HEAD compare.

  The per-version links below stay on upstream on purpose: 0.1.0 to 0.3.3
  were released there, and those tags exist nowhere else - repointing them
  at this repository would turn ten working links into ten broken ones.
-->

[Unreleased]: https://github.com/mmnlfrrr/ha-healthbox3/compare/df843cf...master
[0.3.3]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.3.2...0.3.3
[0.3.2]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.3.1...0.3.2
[0.3.1]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.3.0...0.3.1
[0.3.0]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.2.0...0.3.0
[0.2.0]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.1.4...0.2.0
[0.1.4]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.1.3...0.1.4
[0.1.3]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.1.2...0.1.3
[0.1.2]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.1.1...0.1.2
[0.1.1]: https://github.com/TrojanHorsePower/ha-healthbox3/compare/0.1.0...0.1.1
[0.1.0]: https://github.com/TrojanHorsePower/ha-healthbox3/releases/tag/0.1.0
