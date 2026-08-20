# A single retunable VFO for the audio plane

**Date:** 2026-08-19
**Status:** approved, not yet implemented
**Supersedes:** the audio-plane portions of the geographic-station-selection work

## Problem

Switching stations is unusably slow and unreliable — minutes of silence, choppy
audio, dozens of dead channels accumulating on radiod. Every fix during the
2026-08-19 session addressed a real defect and each introduced another, because
all of them managed the same underlying mistake: **the app creates a radiod
channel per station and destroys it on the way out.**

That single choice generates the whole failure set. Creating a channel places
it at the front-end window edge, where the `wfm` demodulator produces nothing.
Destroying one starts a ~20 s asynchronous purge, so re-selecting the same
station re-creates an SSRC radiod is still tearing down and gets a dead
channel. A channel that yields no RTP looks "dropped", so `ManagedStream`
restores it every few seconds, and each restore is a channel create — which
saturates radiod's control socket and makes the next click queue behind it.

## Principle

Let each layer do what it already knows how to do.

- **radiod** owns the front end and the demodulators.
- **ka9q-python** owns SSRCs, channel lifecycle and RTP transport.
- **this app** owns which stations to show, which one the user picked, and
  getting Opus frames into a browser.

Today the app does all three. The redesign gives the bottom two back.

## Scope

**One audio output at a time.** Some SDRs have the bandwidth for several
simultaneous receive channels, but this app serves a single listener. That
assumption is what makes the design simple, and it holds for both Airspys.

Monitoring several channels for *activity* is unaffected — that is sensing, not
listening, and it continues wherever the station set fits the window.

## Architecture

Two objects, currently conflated, are separated:

**The VFO.** One channel with a fixed, app-owned SSRC that never changes for
the life of the process. It carries whatever the user is listening to and is
**retuned, never recreated** — frequency, preset and filter all change in
place. The SSRC is derived once from the app identity and the radiod identity,
not from the station, because an SSRC is only an address: radiod is happy to
change everything else about a channel that already exists.

**Sensors.** The per-station channels that feed SNR to the activity map,
created only when the whole station set fits the receiver's window (see
`RadioController.fits_window`). They are never listened to and never centred:
narrowband demods work correctly parked at the window edge, and only `wfm`
does not.

Conflating these is what let a search sweep away the channel being listened to,
made `active_channels` mean two different things at once, and put channel
teardown in a race with channel creation.

## Switching stations

Centre the window **before** tuning. Order matters: a demodulator that starts
while parked at the window edge does not recover when the window later moves
onto it — measured repeatedly, including with radiod's own `tune` utility.

1. Place the window on the target frequency (anchor retune — one command).
2. `set_frequency` on the VFO. The window is already centred, so radiod has no
   reason to retune, and the channel lands mid-window instead of at the edge.
3. On a mode change only, `set_preset` as well. radiod restarts the demod,
   which is exactly what a preset change should do.

No channel creation, no teardown, no purge wait. Because the SSRC is stable,
the `ManagedStream` and the browser's WebSocket both stay attached across the
switch.

## Protocol

The browser opens **one** audio WebSocket when the user first listens and keeps
it for the session.

```
WS /ws/audio
  → {"tune": <freq_hz>}                     client asks for a station
  ← {"type": "tuned", "freq_hz": N,
     "channels": 1|2}                       server confirms; frames after this
                                            belong to that station
  ← binary                                  one Opus frame per message
  ← {"type": "nosignal", "freq_hz": N}      tuned, but nothing is coming
```

The `tuned` message is the boundary marker: the browser resets its Opus decoder
on receipt, which it must do anyway because channel count can differ between
presets (`wfm` ships `mono = yes` in some ka9q-radio installs and stereo in
others).

The per-frequency route `/ws/audio/{freq_hz}` is removed. Its frequency
validation against `monitored_freqs` moves onto the `tune` message.

## When the demodulator does not come up

`wfm` sometimes fails to start even with the window correctly centred — same
station, same placement, alive on one attempt and silent on the next. After a
tune, wait ~1.5 s for RTP. If none arrives, **re-assert the preset** on the VFO,
which makes radiod restart the demodulator in place, and wait ~1.5 s again. At
most two such attempts.

The retry must **not** destroy and re-create the VFO channel. Doing so
re-creates an SSRC radiod is still purging (~20 s) and yields a dead channel —
the exact race this design removes. Restarting the demod on the existing
channel is a single command with no lifecycle consequences. Measured: a preset
re-assert revived a silent centred channel in one case and not in another,
which is why the attempt count is bounded and the honest outcome is
`nosignal`. The UI shows a brief "tuning…" state so a 3–4 s
worst case reads as the radio working rather than the app hanging. Continued
silence is reported as `nosignal`, which is honest and actionable.

`drop_timeout_sec` no longer drives recovery, so it is set generously (30 s).
Silence is not a dropped stream: a station with no signal is silent while
perfectly healthy, and treating that as a failure is what produced the restore
storms.

## The anchor, and why it is still here

radiod places a channel by reserving `chan->filter.max_IF` (110 kHz for `wfm`)
from the window edge, but `wfm.c` demodulates through a 384 kHz composite path
needing **±192 kHz**. The 81 kHz shortfall means a `wfm` channel placed by
radiod cannot demodulate. `radio.c` even raises the question in a comment two
lines above the placement logic: *"What if the IF is wider than the receiver
can supply?"*

The workaround: create a narrow `am` "anchor" channel positioned so that
radiod's edge-parking of *the anchor* leaves the window centred on our target.
Verified with radiod's own `tune` utility, so this is not a client-side error —
asked plainly for `wfm` at 102.3 MHz with the window elsewhere, radiod placed
it at IF +219.2 kHz and reported `snr=-inf`.

Requirements:

- The anchor lives in **one** function, with the measurement recorded beside
  it. Today the logic is smeared across `focus_on`, `_set_focus`,
  `_reassert_focus` and the stream-restored callback.
- It must pick the side (above or below) that falls **outside** the current
  window; radiod ignores a channel already in range, so the wrong side is a
  silent no-op.
- It is retuned like the VFO, not recreated per station.
- It is squelched shut, and removed when nothing is playing — users see a
  lingering anchor as a mysterious AM station on their radio.

When radiod reserves the demodulator's real bandwidth, deleting this should
touch one function. That fix is worth sending upstream with the measurements
above.

## What this removes

Sticky focus and `_reassert_focus`; anchor side-selection retries; the 1 kHz
SSRC nudge; the idempotence check; channel removal on listener teardown; the
SSRC reuse race and its purge waits; restore storms; and the disconnect race
that left zombie streams. All of them manage a channel lifecycle that ceases to
exist.

## Error handling

- **radiod unreachable at tune time** — report `nosignal` with a distinct
  reason; do not retry in a loop.
- **Front-end edges unprobed** (`fe_low_edge_hz` is None) — skip centring and
  tune anyway. Narrowband presets work uncentred; `wfm` may not, and that is
  reported rather than papered over.
- **`tune` for a frequency not in the current search** — rejected on the
  message, as the route does today.
- **radiod restart mid-session** — the VFO's SSRC is stable, so re-creating it
  is the normal path; `ManagedStream`'s restore handles it.

## Testing

Audio verification is **content-based**. A wide-open squelch streams noise at
full rate, which satisfies any frame-count or RMS check — that error produced a
false "FM works" claim earlier in this project. Compare spectral shape and
envelope variation against a known-good reference: NWR reads voice/hiss ≈ 14
and envelope variation ≈ 0.4; steady hiss reads 0.03.

1. Tune to a station with signal → audio within ~2 s, voice/hiss > 10.
2. Switch to another station → audio within ~2 s, no WebSocket reconnect, no
   channel created or destroyed (verify by counting channels on radiod before
   and after: it must not change).
3. Switch back to the first → same, with no purge wait. This is the case that
   fails today.
4. Switch mode (FM → NWS) → preset changes, sensors rebuild, VFO SSRC is
   unchanged.
5. Hold a silent station for 90 s → no restore storm; channel count stable;
   control socket stays responsive (a concurrent search still completes
   promptly).
6. Stop listening → anchor removed, radiod returns to sensors only.

## Out of scope

- Patching radiod (tracked separately; this design works without it).
- Multiple simultaneous audio outputs.
- Tuning to arbitrary frequencies rather than to listed stations.
- Any change to the geographic station selection, band handling, or the
  frequency strip, which are working and reviewed.
