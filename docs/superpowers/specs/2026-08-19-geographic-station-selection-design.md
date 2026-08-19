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
receiver's limits.

One consequence is accepted explicitly rather than worked around: **activity
across the whole FM band is not observable.** Every monitored station needs a
radiod channel inside the front-end window, and 20 MHz of FM does not fit in
any window this app will meet. NWS is different — its seven channels span
150 kHz and fit inside *either* Airspy's window — so there the activity map
keeps working and keeps meaning something.

## Design

### Bands stay; segments go

Band selection survives only where it names a real amateur band the user has
an opinion about — 2 m, 1.25 m, 70 cm — because that reflects antennas and
interests, not receiver bandwidth. Sub-segmentation (`2m_1…2m_8`,
`fm_1…fm_38`) is removed entirely. Commercial FM gets no control at all: 88–108
MHz is one band.

`segment_band()` and `SEGMENT_FILL` in `sources/base.py` are removed. The
window width they were computed from is still needed, but for a different
decision (below).

### `Source.center_freq_hz()` is deleted

It is already dead: `tune_center()` stopped tuning anything once we established
that radiod owns front-end placement and derives it from channel frequencies.
Keeping a method whose name promises the app chooses a centre frequency invites
the same misunderstanding back. `RadioController.tune_center()` and
`band_center_hz` go with it.

### Monitoring adapts to the measured window

`apply_stations()` gains one decision, made from the span of the stations the
search returned against `usable_bw_hz` (already probed per host from
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

Fit is tested against `usable_bw_hz * 0.8`, the same margin the segment sizing
used, so a station never sits hard against the window edge.

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

## Components touched

| File | Change |
|---|---|
| `sources/base.py` | drop `segment_band`, `SEGMENT_FILL`, `center_freq_hz` |
| `sources/fm.py` | no controls; `list_stations` filters by radius only |
| `sources/repeaters.py` | band control lists 2 m / 1.25 m / 70 cm only |
| `sources/nws.py` | drop `center_freq_hz` |
| `radio_controller.py` | fit decision in `apply_stations`; drop `tune_center` |
| `app.py` | `activity` flag in results; stop calling `tune_center` |
| `frontend/app.js` | permanent labels; hide band control; honour `activity` |

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

## Out of scope

- Scanning the window across a band to sample activity.
- Any change to the anchor mechanism, Opus path, or band-independent channel
  convergence — all working and verified.
- The upstream radiod `set_freq` margin bug, which the anchor works around.
