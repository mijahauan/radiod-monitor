# Geographic station selection

**Date:** 2026-08-19
**Status:** approved, not yet implemented

## Problem

The UI asks the user to choose a *band segment* before it will show them
anything. Those segments are not a radio concept the user cares about — they
are the receiver's front-end window leaking into the interface. Sized to the
connected radio (as they must be, see CLAUDE.md), they fragment absurdly on a
narrow receiver: on the Airspy HF+'s 660.5 kHz window, Commercial FM becomes
**38 segments** and VHF/UHF Repeaters **71**. The user is made to solve the
radio's problem before reaching the thing they actually want, which is "what
can I hear from here, and let me listen to one".

## Principle

The user picks by geography and station identity. The app absorbs the
receiver's limits — and where a limit has visible consequences, it *shows*
them rather than asking the user to act on them. Band segments made the user
solve the radio's problem before reaching the station they wanted. The
frequency strip (below) instead makes the same constraint legible at a glance,
which is the difference between a chore and an explanation.

One consequence is accepted explicitly rather than worked around: **activity
across the whole FM band is not observable.** Every monitored station needs a
radiod channel inside the front-end window, and 20 MHz of FM does not fit in
any window this app will meet. NWS is different — its seven channels span
150 kHz and fit inside *either* Airspy's window — so there the activity map
keeps working and keeps meaning something.

## Design

### Bands stay; segments go

`Source.controls_schema()` loses its `usable_bw_hz` argument: with segments
gone, the remaining controls do not depend on the receiver. `app.py` stops
passing it and stops injecting `usable_bw_hz` into search `params`.

Band selection survives only where it names a real amateur band the user has
an opinion about — 2 m, 1.25 m, 70 cm — because that reflects antennas and
interests, not receiver bandwidth. Sub-segmentation (`2m_1…2m_8`,
`fm_1…fm_38`) is removed entirely. Commercial FM gets no control at all: 88–108
MHz is one band.

`segment_band()` in `sources/base.py` is removed. `SEGMENT_FILL` is kept but
renamed `WINDOW_FILL` and moved to `radio_controller.py`, which is now its only
consumer: it no longer sizes segments, it decides whether a station set fits.

### `Source.center_freq_hz()` is deleted

It is already dead: `tune_center()` stopped tuning anything once we established
that radiod owns front-end placement and derives it from channel frequencies.
Keeping a method whose name promises the app chooses a centre frequency invites
the same misunderstanding back. `RadioController.tune_center()` and
`band_center_hz` go with it.

### Monitoring adapts to the measured window

`apply_stations()` gains one decision, made by comparing the station set's
**span** — `max(freq_hz) - min(freq_hz)`, zero for a single station — against
`usable_bw_hz` (already probed per host from
`FE_LOW_EDGE`/`FE_HIGH_EDGE`):

- **Span fits the window** → create a channel per station, as today. The
  activity monitor reports real SNR and markers go green. NWS always lands
  here; repeaters do on the R2's 4.1 MHz window.
- **Span does not fit** → create no channels. The station list is a directory.
  A channel is created only when the user clicks Listen — `add_listener()`
  already does this — and `focus_on()` anchors the front end on it.

`usable_bw_hz` unknown (probe failed) falls back to `DEFAULT_USABLE_BW_HZ`
(8 MHz) for the comparison, matching today's behaviour when radiod does not
report the edges.

Fit is tested as `span <= usable_bw_hz * WINDOW_FILL` (0.8), leaving margin so
no station sits hard against the window edge — radiod parks channels near the
edge by design, and wfm cannot demodulate there (see CLAUDE.md).

### The results message says which mode applied

`{type: "results"}` gains `activity: true|false`. The frontend uses it to omit
the activity legend when activity cannot be reported, rather than showing a
legend for green markers that will never appear. This informs without asking
the user to do anything.

### Map

Markers carry permanent labels (callsign and frequency) instead of
click-to-reveal popups; the popup keeps the per-source `extra` detail. Green
still means a channel reporting SNR above threshold, so in directory mode
markers simply never go green — honest, and consistent with `activity: false`.

The band control is hidden whenever `controls.bandSegments` is absent, which
after this change means always for FM.

### Frequency strip

The map places stations geographically; it cannot show why only some of them
can be live at once. A horizontal strip spanning the source's band does: each
station is a tick at its frequency, and a shaded box marks the receiver's
current window.

- **Scale** is the source's whole band — 88–108 MHz for FM, 144–148 for 2 m,
  162.400–162.550 for NWS. On NWS the window box is wider than the band, which
  communicates "all of these are live" without a word of explanation. On FM the
  box is ~3% of the strip on an HF+ and ~20% on an R2, so switching receivers
  shows the difference immediately.
- **Interaction** is click-to-select, identical in effect to clicking a map
  marker: anchor the front end there and start audio. The window box then
  slides to the selection. There is no drag: radiod re-derives placement from
  channel frequencies, so a dragged position is not something the app can hold,
  and offering it would imply a control that does not exist.
- **Station ticks** show which are inside the window, so in directory mode the
  strip explains at a glance why one marker is live and the others are not.

**The window position is measured, not assumed.** The activity monitor already
polls on a timer; it gains a `poll_status()` read of `first_lo` and the
front-end edges and broadcasts `{type: "window", low_hz, high_hz, center_hz}`
to the control sockets. Deriving the box from where the app *believes* it put
the window would be a model, and this project has repeatedly found radiod's
actual placement differs from the obvious model — the anchor mechanism exists
precisely because of one such gap. One extra status exchange every 2 s is a
fair price for showing the truth.

If the window cannot be read, the strip omits the box rather than drawing a
guessed one.

## Components touched

| File | Change |
|---|---|
| `sources/base.py` | drop `segment_band` and `center_freq_hz`; `controls_schema()` loses its argument |
| `sources/fm.py` | no controls; `list_stations` filters by radius only |
| `sources/repeaters.py` | band control lists 2 m / 1.25 m / 70 cm only |
| `sources/nws.py` | drop `center_freq_hz` |
| `radio_controller.py` | `WINDOW_FILL` + fit decision in `apply_stations`; drop `tune_center`/`band_center_hz` |
| `app.py` | `activity` flag in results; `window` broadcast; stop calling `tune_center`; stop injecting `usable_bw_hz` |
| `frontend/app.js` | permanent labels; hide band control; honour `activity`; frequency strip |
| `frontend/index.html` | strip container |

## Error handling

- Front-end probe failed → `DEFAULT_USABLE_BW_HZ` for the fit test.
- A stale `band` key from the browser → sources already fall back to a sane
  band rather than failing the search; that behaviour is kept for the
  remaining real bands.
- Directory mode with a station the search did not return → the audio socket
  already rejects frequencies outside `monitored_freqs`.

## Testing

Content-based, not packet-count-based — a wide-open squelch streams noise at
full rate and satisfies any RMS or frame-count check (this cost us a false
"FM works" claim earlier in the project). Compare against a known-good
reference: NWR reads voice/hiss ≈ 14 and envelope variation ≈ 0.4; steady hiss
reads 0.03.

1. NWS search → 7 stations, all channels created, `activity: true`, markers go
   green, audio at voice/hiss ≈ 14.
2. FM search → full station list across 88–108 MHz, no band control,
   `activity: false`, no channels created before a click.
3. Click an FM station → anchored, `first_lo` centres on it, continuous audio.
4. Switch to the R2 → repeaters flip from directory to fully monitored with no
   code change.
5. Stop the app → every channel released, including the anchor.
6. Frequency strip → box position matches `first_lo` read directly from
   radiod; clicking a tick plays that station and moves the box; switching
   from the HF+ to the R2 visibly widens the box.

## Out of scope

- Scanning the window across a band to sample activity.
- Dragging the window box (see above — radiod will not hold a placement that
  no channel justifies).
- Any change to the anchor mechanism, Opus path, or band-independent channel
  convergence — all working and verified.
- The upstream radiod `set_freq` margin bug, which the anchor works around.
