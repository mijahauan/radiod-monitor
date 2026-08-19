# Audio pipeline failure + dependency refresh

## Root cause (proven, not inferred)

radiod@airspyhf-generic runs on this host with `ttl = 0`, so it emits RTP
**only on the loopback interface** (verified with tcpdump: real 69–95 byte
Opus payloads to 239.x:5004 from 127.0.0.1, SNR 28.6 dB — the radio is fine).

`ka9q-python 3.7.1` — the pinned version — joins the multicast group with
`imr_interface = INADDR_ANY`, which the kernel resolves through the routing
table to `enp3s0`. It therefore never sees the loopback-only stream.

Measured, same channel, same moment:
  join on 127.0.0.1  → 401 frames, payloads 69–77 bytes  (audio)
  join on INADDR_ANY → 0 frames                          (silence)

Upstream ka9q-python fixed exactly this in v3.14.1 / v3.15.0; current
`_create_socket()` binds the group and *explicitly joins loopback*, with the
comment "for a co-located TTL=0 radiod that emits only on `lo`".
**The stale dependency is the audio bug.**

## Second, latent failure

The Opus path only works because of an UNCOMMITTED edit to
`../ka9q-python/ka9q/stream.py` (raw-bytes passthrough). Upstream
`parse_rtp_samples()` returns `None` for OPUS and `_process_packet` drops the
packet — so upgrading, or any `git checkout`, silently kills audio with no
error. Passthrough must become a real, committed library API before the bump.

## Tasks

- [x] Back up the uncommitted ka9q-python patch to scratchpad
- [x] Fast-forward ka9q-python main 26a00e3 → origin/main (3.7.1 → 3.24.0)
- [x] Add a first-class raw-payload passthrough API to RadiodStream +
      ManagedStream (replaces the isinstance-sniffing local patch)
- [x] Verify frames arrive from the TTL=0 loopback radiod through ManagedStream
- [x] radiod-monitor: use the new API in audio_streamer.py
- [x] radiod-monitor: bump the ka9q-python pin
- [x] radiod-monitor: default host airspy-status.local → airspyhf-status.local
      (the Airspy R2 is not plugged in; only the HF+ is on USB)
- [x] radiod-monitor: bound the restore-retry loop (it wrote a 485 MB backend.log)
- [x] Verify end-to-end into a real browser decoder
- [x] (found mid-flight) Assert OUTPUT_ENCODING=OPUS and verify the grant
- [x] (found mid-flight) Derive the decoder's channel count from the Opus TOC byte

## A third bug, found while verifying

`ensure_channel(encoding=OPUS)` does **not** get an Opus channel. radiod applies
the preset's default output encoding (s16be) and honours OPUS only from a
follow-up OUTPUT_ENCODING command — measured: the create alone yields
`channel.encoding=2` and 1440-byte PCM payloads; an explicit
`set_output_encoding()` flips the wire to 71–77 byte Opus frames immediately.
Neither call site asserted it, so whether the browser got Opus or PCM depended
on what an earlier session happened to leave on that SSRC. That is the "works
sometimes" half of the symptom.

Ordering matters too: asserting it *after* the receiver starts means the first
packets on the socket are still PCM. The first-frame TOC sniffer then read a PCM
byte and reported stereo for a mono stream. Both call sites now assert before
the receiver starts and verify the grant.

## Review

**Root cause of "no audio in the browser": a stale dependency.** radiod here is
co-located and `ttl = 0`, so it emits only on `lo`; ka9q-python 3.7.1 joined the
group on the routed interface and never saw a packet. The socket was healthy and
silent — no error anywhere, which is why this was hard to see from the app side.

**Changes**

*ka9q-python* (3.7.1 → 3.24.0, plus a new API):
- `RadiodStream(raw_payloads=True)` / `ManagedStream(raw_payloads=True)` — a
  committed, documented transport mode for framed encodings (OPUS/OPUS_VOIP/
  AX25), delivering `List[bytes]` with the resequencer bypassed. Replaces the
  uncommitted local patch, which sniffed `isinstance(samples, bytes)` and would
  have been erased by any checkout, silently killing audio.
- Guarded `stop()` so the resequencer flush cannot mix an ndarray into a buffer
  of codec frames.
- Updated the pinned signature in `tests/client_usage_manifest.json` (the change
  is additive: a keyword arg with a default).

*radiod-monitor*:
- `audio_streamer.py` — `raw_payloads=True`; assert + verify the Opus grant
  before the receiver starts and again after every restore; derive the channel
  count from the first frame's TOC byte and send it as a `config` message; drop
  and loudly log any payload >1275 bytes (RFC 6716's single-frame maximum), so a
  lost grant can never again masquerade as audio; bounded restore retries.
- `app.py` / `radio_controller.py` — forward the `config` text message; assert
  the grant on the control-plane path too; default host → `airspyhf-status.local`.
- `frontend/app.js` — configure `AudioDecoder` from the `config` message rather
  than from a preset-derived guess.
- `radiod-monitor.sh` — rotate `backend.log` past 32 MB.
- `pyproject.toml` — `ka9q-python>=3.24.0` (do not lower: the loopback join).

**Verification** — through the running server, over real WebSockets, decoded
with a real Opus decoder: `{"type":"config","channels":1}` then 287 binary
messages, 101–203 bytes each, **287/287 decoded, 0 errors, 5.74 s of continuous
audio** (peak 1.0, rms 0.021) from a 5.74 s capture. Before the fix the same
path delivered 0 frames.

**Not fixed, needs hardware.** `radiod@airspy-generic` crash-loops with
`AIRSPY_ERROR_NOT_FOUND` (49,931 restarts) — the wideband Airspy R2 is not
plugged in; only the HF+ is on USB. All three shipped sources are VHF, and the
HF+ has 768 kHz of front-end bandwidth against `wfm`'s 384 kHz downconverter and
`FmSource`'s 5 MHz band segments. VHF tunes but has no signal on an HF antenna.
So the pipeline is proven on HF; FM/NWS/repeaters need the R2 reconnected.

**Left for the user:** the ka9q-python changes are uncommitted. That is the exact
fragility this work removed, so they should be committed and released before the
next `pip install`. `backend.log.1` (485 MB, the old flood) is still on disk.

## Follow-up found during final verification

**Time-to-first-audio is 4–13 s, and it is not the audio plane.** Every frame
that arrives decodes (302/302, zero errors, repeatedly); the stream simply
starts late. The cost is in `_apply_stations_locked`: every search removes
*all* channels on the shared destination and then polls up to 10 s for radiod
to purge the resulting freq=0 zombies — the "Timed out waiting for radiod to
purge zombie channels" warning fires in practice — before recreating nine
channels. Clicking Listen right after a search therefore waits on a teardown
that just destroyed the channel it is about to recreate.

**Fixed** by converging with a diff instead of a wipe-and-rebuild. The SSRC is
a deterministic hash of the parameters that define a channel, so the wanted
SSRC set is computable before talking to radiod (`allocate_ssrc`, verified to
produce byte-identical values to `ensure_channel`). A channel whose SSRC is in
that set is correct by construction and is left untouched.

That is what makes the blocking wait unnecessary rather than merely shorter:
removals and creations are now disjoint by definition, so there is no
remove-then-immediately-recreate race for radiod's asynchronous teardown to
lose. The removals became fire-and-forget and the 10 s purge poll is gone.

Measured, time-to-first-audio on a frequency in the station set:

| | before | after |
|---|---|---|
| cold (channels must be created) | 3.66 s | 1.15 s |
| repeat search (channels reused) | 7.09 s, 12.64 s | 0.07 s, 0.05 s |

300+ frames per run, zero decode errors throughout; logs confirm the reuse
path ("Reused 6 existing channel(s); creating 0") and zero zombie waits.
A search no longer cuts off audio the user is listening to.

Two things I changed in this area while chasing it:

- Dropped the per-station `verify_channel()` from `apply_stations`. It cost a
  full `discover_channels()` poll *per station* — nine per NWS search — for a
  check that gates nothing on that path. This one is a clear win.
- Dropped `verify_channel()` from `_assert_opus` too, on the theory that it was
  adding ~1.5 s to the listen path. Honest caveat: the measurements were
  dominated by the teardown variance above and did **not** isolate a win. The
  change still stands on its own merits — the wire-level check in `_broadcast`
  (reject any payload too large to be an Opus frame) inspects the actual bytes,
  so it is strictly stronger than re-reading the control plane, and free.
