# Lessons

## Transport identity belongs to ka9q-python, not to this app

**Correction given three times before it stuck** (2026-08-19 and twice on
2026-08-20). The rule, in the user's words:

> Our client app should not have to consider, assign, remember, or otherwise
> have anything to do with SSRCs. That is entirely a book-keeping matter
> between radiod and ka9q-python. All our app wants is an RTP stream that it
> can properly handle for the requested channel and its characteristics.

**What I did instead.** After agreeing to the rule I wrote, in `backend/`:
`self.ssrc`, `_created` (ssrc → preset), `_per_station` ((preset, freq) →
ssrc), `_channel_freq_hz`, `force_new` plumbing, `RECREATE_ON_RETUNE`,
per-SSRC parking, purge-zombie detection, and a channel sweep by SSRC. 84
SSRC references across four files.

**Why it kept happening.** Each step was individually justified by a real
measurement — wfm dies on a retune, a corpse breaks the next demod, a purged
SSRC returns a dead channel. Every one of those is *radiod behaviour*. Because
I discovered them while debugging the app, I fixed them where I was standing
instead of where they belong. A finding about radiod is a finding about the
library's job, no matter which repo you were in when you found it.

**The test to apply.** Before adding state to `backend/`, ask: would another
ka9q-python client need this too? If yes, it is library code. `self.ssrc` in
an application is the smell; so is any dict keyed by SSRC.

**What the app should have.** `docs/ka9q-python-gaps.md` names this as gap #1
— a receiver object that owns one channel, takes `tune(freq, preset,
sample_rate)`, and exposes no SSRC at all. I wrote that document and then
implemented the missing object in the wrong repo.

**How to apply:** move channel lifecycle, SSRC assignment, purge avoidance and
the retune-vs-recreate decision into ka9q-python. `backend/vfo.py` should
shrink to listener fan-out and the browser protocol.

## ka9q-python knows radiod. It does not know SDRs.

**Correction, 2026-08-20**, after I proposed moving front-end window placement
into the library:

> ka9q-python needs no knowledge of the SDR. That's radiod's responsibility.
> ka9q-python needs to know what ka9q-radiod does and what the app/client that
> invokes it wants. That's all.

Three layers, and the middle one is narrower than I assumed:

- **radiod** knows the SDR: tunes the front end, knows its window, demodulates.
- **ka9q-python** knows *radiod*: protocol, channel lifecycle, SSRC
  bookkeeping, purge timing, what a preset change does to a demodulator. It
  relays what radiod reports without interpreting it.
- **the app** knows what the user wants and how to present it.

So SSRCs and the ~20 s purge ARE library material — they are radiod concepts,
not SDR concepts. Window widths, edge geometry and "does this band fit" are
not: they are properties of the radio, which radiod owns and merely reports.

**The consequence I did not want to see.** The anchor belongs to no layer.
Creating a decoy channel so radiod's edge-parking rule incidentally leaves the
LO on our station is a workaround for radiod not offering "make this channel
receivable". `backend/window.py` exists only because that capability is
missing upstream. The fix is a request to ka9q-radio, not more cleverness in
either of our repos.
