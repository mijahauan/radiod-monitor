"""
Source protocol — the plugin contract for frequency providers.

A Source knows how to:
  - advertise its UI controls (controls_schema)
  - return a filtered list of Station objects to monitor (list_stations)

The shared pipeline (control WS, RadioController.ensure_channel loop,
activity monitor, audio streamer) is source-agnostic — it only sees Station
objects. Which stations are simultaneously receivable is RadioController's
problem, not a Source's -- see RadioController for how it decides what the
connected radio can currently cover.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class Station:
    """
    One monitorable transmitter.

    `id` is a stable identifier used for per-station UI state; `extra` is a
    free-form dict of source-specific fields that the frontend renders into
    the marker popup and list row (e.g. {"channel": "CH 5", "note": "..."}
    for NWS, or {"offset": "-0.6", "tone": "100.0 Hz"} for a repeater).
    """
    id: str
    name: str
    freq_hz: float
    lat: float
    lon: float
    distance_km: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "freq_hz": self.freq_hz,
            "lat": self.lat,
            "lon": self.lon,
            "distance_km": round(self.distance_km, 2),
            "extra": self.extra,
        }


class Source:
    """
    Subclass contract. All methods receive a `params` dict containing the
    per-source control values the user selected in the UI; the valid keys
    are defined by the source's `controls_schema`.
    """

    # Registry key — must be unique across sources. Lowercase, no spaces.
    key: str = ""

    # Display name shown in the UI mode selector.
    display_name: str = ""

    # Demod preset passed to radiod ensure_channel. Most sources use nfm;
    # airband sources would use "am", HF for "usb"/"lsb", broadcast FM
    # uses "wfm" (which radiod forces to 48 kHz output).
    preset: str = "nfm"

    # Output sample rate radiod emits for this channel. This is the *output*
    # rate (post-demod), not the demodulator IF rate — radiod's samprate
    # setting in presets.conf is internal. wfm output is forced to 48 kHz by
    # the demodulator regardless of the requested rate, and nfm/am default
    # to 24 kHz/12 kHz but 48 kHz works and simplifies the pipeline.
    sample_rate: int = 48000

    # Channel count hint for the UI. It is NOT what configures the decoder:
    # a source cannot know the answer, because the wfm preset ships
    # `mono = yes` in some ka9q-radio installs and stereo in others, and
    # ChannelInfo carries no channel count. The VFO (backend/vfo.py) reads
    # the truth from the first Opus frame's TOC byte and sends it to the
    # browser -- see vfo.opus_channels().
    audio_channels: int = 1

    # Whether SNR-based squelch is meaningful for this source. ka9q-radio's
    # wfm demodulator does not publish a valid SNR, so SNR-squelched wfm
    # channels never open and emit zero RTP packets — audio streams time out.
    # Narrowband modes (nfm/am/etc) do publish SNR and benefit from squelch.
    # When False, the controller leaves squelch wide open (power-based, very
    # low threshold) so audio always flows.
    snr_squelch: bool = True

    def controls_schema(self) -> Dict[str, Any]:
        """
        Return a JSON-serializable description of this source's UI controls.

        Currently supported keys:
          - `bandSegments`: [{value, label}, ...] — if present, the frontend
            shows a band dropdown and passes the selected value back in
            params["band"]. Use it only for bands the user has an opinion
            about (2m vs 70cm), never to subdivide a band to fit the
            receiver: that is the app's problem, not the user's, and
            RadioController solves it by monitoring what fits and treating
            the rest as a directory.
          - `defaultBand`: string — initial selection.

        Return an empty dict for sources with no extra controls.
        """
        return {}

    def list_stations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        params: Dict[str, Any],
    ) -> List[Station]:
        """Return the filtered station list for this (location, radius, params)."""
        raise NotImplementedError
