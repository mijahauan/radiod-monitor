# What ka9q-python is missing, from this app's point of view

Notes for a future upstream contribution. Nothing here blocks radiod-monitor —
each gap has a workaround in `backend/` — but each workaround is application
code doing transport work, which is the wrong side of the line.

The principle: *an application should say what frequency, preset, and sample
rate it wants. Everything below that — SSRCs, multicast addresses, channel
lifecycle, front-end placement — belongs to the library.*

## 1. There is no receiver that follows a channel

`RadiodStream` binds to a `ChannelInfo` and filters on its SSRC, so it follows
a retune correctly, but it has no restoration: if radiod restarts, the stream
goes quiet and stays quiet.

`ManagedStream` adds restoration, but binds to *parameters* rather than to a
channel. It calls `ensure_channel(frequency_hz=…, preset=…)` at start and again
on every restore (`managed_stream.py:237,421`), deriving the SSRC from those
parameters each time. So it cannot follow a retune: change the channel's
frequency and the next restore computes the *old* frequency's SSRC, creates a
second channel, and delivers from the wrong one.

That leaves no class for the common case — one long-lived receiver the user
retunes, which is what every SDR front panel has been since 1930.

**What would fill it:** a `Vfo`/`Receiver` object owning one channel for its
lifetime, with `tune(freq_hz, preset=None, sample_rate=None)`, restoration keyed
on the *channel's* continued existence rather than on RTP silence, and no SSRC
in its public API at all.

**Workaround here:** `backend/vfo.py` — `RadiodStream` plus an explicit
existence check on each tune.

## 2. Restoration cannot distinguish silence from death

`ManagedStream`'s `drop_timeout_sec` treats "no RTP for N seconds" as a lost
stream. For a squelched or simply idle station that is normal operation, so the
restore loop fires on healthy channels. Each restore is a channel create; on a
receiver with several channels this saturates radiod's control socket, and in
this project it produced 39 channels where 1 was wanted.

The channel's own status says whether it still exists — `poll_channel(ssrc)` is
O(1) and answers definitively. Restoration should ask that, not a timer on the
data plane.

## 3. The front-end window is invisible

radiod publishes `FE_LOW_EDGE`/`FE_HIGH_EDGE` and `FIRST_LO_FREQUENCY`, so where
the window sits and how wide it is are knowable — but only per channel, and only
by parsing status yourself. An app that wants to know "can this receiver hear
102.3 MHz right now?" has to probe with a throwaway channel to get the edges at
all (the limits ride along with *per-channel* status), then track `first_lo`
itself.

**What would fill it:** front-end state on the control object —
`control.frontend.window` returning `(low_hz, high_hz)` and refreshing on poll —
plus a documented answer to "how do I make radiod put the window where I want
it?", which today is the undocumented trick of creating a channel positioned so
that radiod's own edge-parking rule centres the window where you need it.

**Workaround here:** `backend/frontend_window.py`.

## 4. `ensure_channel()` cannot take an SSRC

`create_channel()` accepts `ssrc=` and auto-allocates when it is omitted;
`ensure_channel()` accepts no `ssrc` at all and always derives one from the
parameters. So "make sure *this* channel exists, whatever it is currently tuned
to" is not expressible. It is the same parameters-versus-identity confusion as
(1).

## 5. `demod_type` was derived from a preset-name allowlist

Fixed in 3.25.1 — recorded here because the failure mode is worth remembering.
The library inferred the demodulator from a five-name allowlist and sent
`DEMOD_TYPE` right after `PRESET` in the same packet, so any preset outside the
list got the wrong demodulator, silently overriding what radiod had just loaded
from `presets.conf`. `wfm` ran the narrowband FM demod behind a ±110 kHz filter:
no output, `snr=-inf`, no error.

Deriving in the client what the server already knows is the general shape of
this bug. radiod reads the preset file; it does not need to be told what the
preset means.
