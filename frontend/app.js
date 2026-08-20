/*
 * radiod-monitor frontend.
 *
 * Single-page app with a sidebar (host selector, mode selector, per-mode
 * controls, location/squelch/radius, station list, audio panel) and a
 * Leaflet map. Backend contract lives in backend/app.py — see README.md.
 */

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------
const hostSelect       = document.getElementById('hostSelect');
const hostRefreshBtn   = document.getElementById('hostRefreshBtn');
const modeSelect       = document.getElementById('modeSelect');
const modeControls     = document.getElementById('modeControls');
const locationInput    = document.getElementById('locationInput');
const searchBtn        = document.getElementById('searchBtn');
const squelchSlider    = document.getElementById('squelchSlider');
const squelchValue     = document.getElementById('squelchValue');
const radiusSlider     = document.getElementById('radiusSlider');
const radiusValue      = document.getElementById('radiusValue');
const wsStatus         = document.getElementById('ws-status');
const wsText           = document.getElementById('ws-text');
const repeaterList     = document.getElementById('repeaterList');
const repeaterCount    = document.getElementById('repeaterCount');

const audioPanel             = document.getElementById('audio-panel');
const stopAudioBtn           = document.getElementById('stopAudioBtn');
const audioRepeaterCallsign  = document.getElementById('audio-repeater-callsign');
const audioRepeaterFreq      = document.getElementById('audio-repeater-freq');
const resumeAudioBtn         = document.getElementById('resumeAudioBtn');

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let map;
let markers = {};           // freq_hz -> Leaflet Marker
let stationsData = [];
let wsControl;
let currentMode = '';
let currentActivityAvailable = true;
let sourcesSchema = {};     // key -> {display_name, controls, preset, audio_channels}

// Marker icons
const defaultIcon = L.divIcon({
    className: 'custom-icon',
    html: `<div style="width:16px;height:16px;background:#3b82f6;border-radius:50%;border:2px solid #fff;box-shadow:0 0 5px rgba(0,0,0,0.5);"></div>`,
    iconSize: [16, 16],
});
const activeIcon = L.divIcon({
    className: 'custom-icon',
    html: `<div style="width:20px;height:20px;background:#10b981;border-radius:50%;border:2px solid #fff;box-shadow:0 0 15px #10b981;"></div>`,
    iconSize: [20, 20],
});

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------
function initMap() {
    map = L.map('map').setView([38.5, -91.0], 5);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap &copy; CARTO',
        subdomains: 'abcd',
        maxZoom: 19,
    }).addTo(map);
}

// ---------------------------------------------------------------------------
// Radiod host discovery & selection
// ---------------------------------------------------------------------------
async function discoverHosts() {
    hostSelect.innerHTML = '<option value="">Discovering…</option>';
    try {
        const res = await fetch('/api/radiod/discover');
        const data = await res.json();
        hostSelect.innerHTML = '';

        const hosts = data.hosts || [];
        const current = data.current || '';

        if (hosts.length === 0) {
            // No mDNS services — still show the current host so the user
            // can scan with it.
            const opt = document.createElement('option');
            opt.value = current;
            opt.textContent = current || '(no radiod discovered)';
            opt.selected = true;
            hostSelect.appendChild(opt);
        } else {
            let matched = false;
            for (const h of hosts) {
                const opt = document.createElement('option');
                opt.value = h.address;
                opt.textContent = `${h.name}  (${h.address})`;
                if (h.address === current || h.name.includes(current)) {
                    opt.selected = true;
                    matched = true;
                }
                hostSelect.appendChild(opt);
            }
            // Always insert the current host at the top if we didn't match
            // one of the discovered ones, so the UI reflects reality.
            if (!matched && current) {
                const opt = document.createElement('option');
                opt.value = current;
                opt.textContent = `${current} (current)`;
                opt.selected = true;
                hostSelect.insertBefore(opt, hostSelect.firstChild);
            }
        }
        console.log(`Found ${hosts.length} radiod instance(s); current: ${current}`);
    } catch (e) {
        console.error('Host discovery failed:', e);
        hostSelect.innerHTML = '<option value="">Discovery failed</option>';
    }
}

async function selectHost(address) {
    if (!address) return;
    try {
        const res = await fetch('/api/radiod/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ host: address }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert(`Cannot switch to ${address}: ${err.detail || res.statusText}`);
            return;
        }
        console.log(`Switched to radiod: ${address}`);
        // Clear any in-flight audio and any stale markers, then re-scan.
        if (audioSession) { audioSession.stop(); audioSession = null; }
        audioPanel.classList.add('hidden');
        clearStations();
        triggerSearch();
    } catch (e) {
        console.error('selectHost failed:', e);
        alert('Radiod switch failed. See console.');
    }
}

// ---------------------------------------------------------------------------
// Source / mode configuration
// ---------------------------------------------------------------------------
async function loadSources() {
    modeSelect.innerHTML = '<option value="">Loading…</option>';
    try {
        const res = await fetch('/api/sources');
        const data = await res.json();
        sourcesSchema = {};
        modeSelect.innerHTML = '';
        for (const s of data.sources || []) {
            sourcesSchema[s.key] = s;
            const opt = document.createElement('option');
            opt.value = s.key;
            opt.textContent = s.display_name;
            modeSelect.appendChild(opt);
        }
        // Pick a default: persist across reloads via localStorage.
        const saved = localStorage.getItem('radiod-monitor.mode');
        if (saved && sourcesSchema[saved]) {
            modeSelect.value = saved;
        }
        currentMode = modeSelect.value || (data.sources && data.sources[0] && data.sources[0].key) || '';
        renderModeControls();
    } catch (e) {
        console.error('loadSources failed:', e);
        modeSelect.innerHTML = '<option value="">Failed</option>';
    }
}

/**
 * Build per-source UI controls inside #modeControls based on the schema
 * returned by /api/sources. Currently supports:
 *   - bandSegments: [{value, label}]  → dropdown
 */
function renderModeControls() {
    modeControls.innerHTML = '';
    const schema = sourcesSchema[currentMode];
    if (!schema || !schema.controls) return;
    const c = schema.controls;

    if (Array.isArray(c.bandSegments) && c.bandSegments.length > 0) {
        const wrap = document.createElement('div');
        wrap.className = 'control-group';
        const label = document.createElement('label');
        label.setAttribute('for', 'bandSelect');
        label.textContent = 'Band';
        const sel = document.createElement('select');
        sel.id = 'bandSelect';
        for (const bs of c.bandSegments) {
            const opt = document.createElement('option');
            opt.value = bs.value;
            opt.textContent = bs.label;
            sel.appendChild(opt);
        }
        sel.value = c.defaultBand || c.bandSegments[0].value;
        sel.addEventListener('change', () => triggerSearch());
        wrap.appendChild(label);
        wrap.appendChild(sel);
        modeControls.appendChild(wrap);
    }
}

function currentParams() {
    // Collect per-source param values from the injected controls.
    const params = {};
    const bandSel = document.getElementById('bandSelect');
    if (bandSel) params.band = bandSel.value;
    return params;
}

// ---------------------------------------------------------------------------
// Control WebSocket
// ---------------------------------------------------------------------------
function connectControl() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    wsControl = new WebSocket(`${proto}//${window.location.host}/ws/control`);

    wsControl.onopen = () => {
        wsStatus.className = 'status-dot connected';
        wsText.textContent = 'Connected';
        // Only search if we already know what mode to use.
        if (currentMode) triggerSearch();
    };
    wsControl.onclose = () => {
        wsStatus.className = 'status-dot disconnected';
        wsText.textContent = 'Disconnected — retrying…';
        setTimeout(connectControl, 3000);
    };
    wsControl.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        if (data.type === 'results')        handleSearchResults(data);
        else if (data.type === 'activity')  handleActivityUpdate(data);
        else if (data.type === 'window') {
            currentWindow = { low_hz: data.low_hz, high_hz: data.high_hz };
            drawFreqStrip();
        }
        else if (data.type === 'error')     alert(data.message);
    };
}

function triggerSearch() {
    if (!wsControl || wsControl.readyState !== WebSocket.OPEN) return;
    if (!currentMode) return;

    clearStations();

    wsControl.send(JSON.stringify({
        type: 'search',
        mode: currentMode,
        location: locationInput.value,
        squelch: parseFloat(squelchSlider.value),
        radius: parseFloat(radiusSlider.value),
        params: currentParams(),
    }));
}

function clearStations() {
    for (const id in markers) map.removeLayer(markers[id]);
    markers = {};
    repeaterList.innerHTML = '';
    repeaterCount.textContent = '0';
    // A mode switch that leaves no channels and no anchor (e.g. NWS -> FM in
    // directory mode) produces no further "window" broadcasts at all, so a
    // stale window caption/box would otherwise persist indefinitely and
    // suppress the "wider than the receiver" caption. Reset it here so the
    // strip reflects "no measurement yet" until a new one arrives.
    currentWindow = null;
    drawFreqStrip();
}

// ---------------------------------------------------------------------------
// Search results + activity
// ---------------------------------------------------------------------------
function handleSearchResults(data) {
    stationsData = data.stations || [];
    repeaterCount.textContent = stationsData.length;
    // False when the station set is wider than the receiver's window: no
    // channels exist, so no marker will ever go green. There is no activity
    // legend in this UI to hide, so the flag's only consumer is the frequency
    // strip caption (Task 5), which explains the situation in one line.
    currentActivityAvailable = data.activity !== false;

    if (data.lat !== undefined && data.lon !== undefined) {
        map.setView([data.lat, data.lon], 9);
        L.marker([data.lat, data.lon], {
            icon: L.divIcon({
                className: 'custom-icon',
                html: `<div style="width:12px;height:12px;background:#ef4444;border-radius:50%;border:2px solid #fff;"></div>`,
            }),
        }).addTo(map).bindPopup('Your Location');
    }

    for (const st of stationsData) {
        const marker = L.marker([st.lat, st.lon], { icon: defaultIcon }).addTo(map);
        marker.bindPopup(buildPopup(st));
        marker.bindTooltip(
            `${st.name} ${(st.freq_hz / 1e6).toFixed(1)}`,
            { permanent: true, direction: 'right', className: 'station-label' }
        );
        markers[st.freq_hz] = marker;

        const li = document.createElement('li');
        li.className = 'repeater-item';
        li.id = `rep-${st.freq_hz}`;
        li.innerHTML = `
            <div class="rep-header">
                <span class="rep-call">${escapeHtml(st.name)}</span>
                <span class="rep-freq">${(st.freq_hz / 1e6).toFixed(3)}</span>
            </div>
            <div class="rep-details">
                <span>Dist: ${st.distance_km.toFixed(1)} km</span>
                <span class="rep-snr" id="snr-${st.freq_hz}"
                    style="margin-left:auto;font-size:0.75rem;color:#9ca3af;">SNR: --</span>
            </div>
            <div class="signal-meter">
                <div class="signal-fill" id="sig-${st.freq_hz}"></div>
            </div>
        `;
        li.onclick = () => { map.setView([st.lat, st.lon], 12); marker.openPopup(); };
        repeaterList.appendChild(li);
    }
    drawFreqStrip();
}

function buildPopup(st) {
    // Build the popup as a real DOM element rather than an HTML string so
    // we can attach a proper click handler. Interpolating st.name into an
    // inline onclick="..." attribute breaks as soon as the name contains
    // anything that clashes with the outer quoting.
    const root = document.createElement('div');
    root.className = 'dark-popup';

    const h = document.createElement('h4');
    h.textContent = st.name;
    root.appendChild(h);

    const freqP = document.createElement('p');
    freqP.innerHTML = `<strong>Freq:</strong> ${(st.freq_hz / 1e6).toFixed(3)} MHz`;
    root.appendChild(freqP);

    for (const [k, v] of Object.entries(st.extra || {})) {
        if (v === '' || v == null) continue;
        const p = document.createElement('p');
        const strong = document.createElement('strong');
        strong.textContent = `${k}: `;
        p.appendChild(strong);
        p.appendChild(document.createTextNode(String(v)));
        root.appendChild(p);
    }

    const btn = document.createElement('button');
    btn.textContent = 'Listen Live';
    btn.style.cssText = 'margin-top:10px; padding:5px; font-size:0.8rem;';
    btn.addEventListener('click', () => listenToStation(st.freq_hz));
    root.appendChild(btn);

    return root;
}

function handleActivityUpdate(data) {
    const freq = data.freq;
    const isAct = data.isActive;
    const snr = data.snr !== null && data.snr !== undefined ? parseFloat(data.snr) : null;

    const li      = document.getElementById(`rep-${freq}`);
    const sigFill = document.getElementById(`sig-${freq}`);
    const snrLbl  = document.getElementById(`snr-${freq}`);
    const marker  = markers[freq];
    if (!(li && sigFill && marker)) return;

    if (isAct) {
        li.classList.add('active-signal');
        marker.setIcon(activeIcon);
    } else {
        li.classList.remove('active-signal');
        marker.setIcon(defaultIcon);
    }

    if (snr !== null && isFinite(snr)) {
        const pct = Math.max(0, Math.min(100, (snr / 30) * 100));
        sigFill.style.width = `${pct}%`;
        if (snrLbl) {
            const color = snr >= 10 ? '#10b981' : snr >= 3 ? '#f59e0b' : '#6b7280';
            snrLbl.textContent = `SNR: ${snr.toFixed(1)} dB`;
            snrLbl.style.color = color;
        }
        marker.unbindTooltip();
        marker.bindTooltip(`${snr.toFixed(1)} dB SNR`, {
            permanent: false, direction: 'top', className: 'snr-tooltip',
        });
    } else {
        sigFill.style.width = '0%';
        if (snrLbl) {
            snrLbl.textContent = 'SNR: --';
            snrLbl.style.color = '#6b7280';
        }
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );
}

// ---------------------------------------------------------------------------
// Audio — one persistent WebSocket + WebCodecs AudioDecoder, retuned
// underneath by sending {tune: freqHz}. The server never tears down the
// socket to change stations; each "tuned" message is a decoder-reset
// boundary because Opus decoder state is per-stream and the channel count
// can differ between presets (mono NWS vs stereo FM).
// ---------------------------------------------------------------------------
const AUDIO_SAMPLE_RATE = 48000;
const MIN_BUFFER_PACKETS = 5;   // ~100 ms jitter buffer
const START_DELAY_SEC = 0.15;

function setAudioStatus(text) {
    if (audioRepeaterFreq) audioRepeaterFreq.textContent = text;
}

// Looks up a station's display name by frequency the same way markers are
// keyed (markers[freq_hz]) -- ground truth for "what is actually playing"
// is the frequency the backend just confirmed via "tuned", not whatever name
// was passed to tune() at click time, which can go stale under rapid clicks.
function stationNameForFreq(freqHz) {
    const st = stationsData.find(s => s.freq_hz === freqHz);
    return st ? st.name : `${(freqHz / 1e6).toFixed(3)} MHz`;
}

class AudioSession {
    constructor() {
        this.freqHz = null;
        this.numChannels = 1;
        this.ws = null;
        this._ready = null;      // promise resolved once this.ws is OPEN
        this.audioContext = null;
        this.decoder = null;
        this.pending = [];
        this.nextPlayTime = 0;
        this.playing = false;
        this.stopped = false;
        this.tuning = false;
    }

    async start() {
        if (!window.AudioDecoder) {
            throw new Error('WebCodecs AudioDecoder unavailable. Requires HTTPS or localhost in Chrome/Firefox.');
        }
        this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: AUDIO_SAMPLE_RATE,
        });
        if (this.audioContext.state === 'suspended') {
            try { await this.audioContext.resume(); } catch (_) {}
        }
        if (this.audioContext.state !== 'running') {
            // Autoplay policy refused the resume even inside the click
            // gesture that started this session -- give the user the manual
            // fallback rather than leaving playback silently stalled.
            resumeAudioBtn.classList.remove('hidden');
        }
        this._rxCount = 0;
        this._decodeErrCount = 0;
        // The decoder is (re)created per "tuned" message, not here — there is
        // no station yet, so no channel count to configure it with.
        this._openSocket();
    }

    // Opens the WebSocket and wires its handlers. Split out from start() so
    // tune() can call it again on demand if the socket has died -- there is
    // no background reconnect timer, so this is the only path back to a live
    // socket, and it never runs concurrently with another open socket
    // because it is only invoked when this.ws is null/CLOSED/CLOSING.
    _openSocket() {
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${proto}//${window.location.host}/ws/audio`);
        this.ws.binaryType = 'arraybuffer';
        this._ready = new Promise((resolve) => {
            this.ws.onopen = () => { console.log('Audio WS open'); resolve(); };
        });
        this.ws.onerror = (e) => console.warn('Audio WS error', e);
        this.ws.onclose = () => {
            console.log('Audio WS closed');
            this.ws = null;
            this._ready = null;
            // Without this the session sits on a dead socket forever: every
            // later Listen click would hit the same silent "not open" guard
            // tune() has to protect against. Surface it and let tune()
            // reopen on the next request instead.
            if (!this.stopped) setAudioStatus('audio connection lost — click Listen to reconnect');
        };
        this.ws.onmessage = (ev) => {
            if (this.stopped) return;
            if (typeof ev.data === 'string') {
                this._onControlMessage(ev.data);
                return;
            }
            if (!(ev.data instanceof ArrayBuffer)) return;
            if (!this.decoder || this.decoder.state !== 'configured') {
                // Frames that arrive before the first "tuned" message (or
                // between a tune and its decoder reset) have no decoder to
                // go to. Opus frames are self-contained, so dropping the
                // first few costs at most a few ms of audio.
                return;
            }
            if (this.tuning) {
                // The backend starts streaming RTP from the new station as
                // soon as it appears -- up to its ~1.5s settle window --
                // before it broadcasts "tuned". The decoder is still
                // configured for the *previous* station's channel count
                // until that message resets it, so a mono->stereo (or
                // stereo->mono) retune would otherwise feed frames of the
                // wrong channel count to a decoder that will throw on them.
                // This is not cosmetic: it is the boundary that keeps a
                // retune from crashing the decoder.
                return;
            }
            this._rxCount++;
            if (this._rxCount <= 3 || this._rxCount % 100 === 0) {
                console.log(`rx #${this._rxCount} ${this.freqHz/1e6} MHz: ${ev.data.byteLength} bytes, decoder.state=${this.decoder.state}, queue=${this.decoder.decodeQueueSize}`);
            }
            try {
                this.decoder.decode(new EncodedAudioChunk({
                    type: 'key',       // Opus frames are self-contained
                    timestamp: this._rxCount * 20000,  // 20 ms per frame in µs
                    data: new Uint8Array(ev.data),
                }));
            } catch (e) {
                this._decodeErrCount++;
                if (this._decodeErrCount <= 3) console.warn('decode error', e);
            }
        };
    }

    async tune(freqHz) {
        this.tuning = true;
        this.pending = [];
        this.playing = false;
        if (!this.ws || this.ws.readyState === WebSocket.CLOSED || this.ws.readyState === WebSocket.CLOSING) {
            this._openSocket();
        }
        await this._ready;
        if (this.stopped) return;
        this.ws.send(JSON.stringify({ tune: freqHz }));
    }

    _onControlMessage(text) {
        let msg;
        try { msg = JSON.parse(text); } catch (_) { return; }
        if (msg.type === 'tuned') {
            this.freqHz = msg.freq_hz;
            this._resetDecoder(msg.channels || 1);
            // Cleared only after the decoder has been reconfigured for the
            // new station, so it stays true across the whole window in
            // which stray frames of the old channel count could still
            // arrive (see the "tuning" guard in onmessage above).
            this.tuning = false;
            // The name comes from a fresh lookup by freq_hz, not from
            // whatever the caller of tune() said at click time -- a second
            // Listen click before this "tuned" arrives would otherwise
            // caption station A with station B's name. Falls back to the
            // frequency when the station isn't in the current result set
            // (e.g. a second tab joining a stream someone else tuned).
            audioRepeaterCallsign.textContent = stationNameForFreq(msg.freq_hz);
            setAudioStatus(`${(msg.freq_hz/1e6).toFixed(3)} MHz`);
        } else if (msg.type === 'nosignal') {
            this.tuning = false;
            setAudioStatus(`no signal on ${(msg.freq_hz/1e6).toFixed(3)} MHz`);
        } else if (msg.type === 'error') {
            this.tuning = false;
            setAudioStatus(msg.message);
        }
    }

    _resetDecoder(channels) {
        if (this.decoder && this.decoder.state !== 'closed') {
            try { this.decoder.close(); } catch (_) {}
        }
        this.pending = [];
        this.playing = false;
        this.decoder = new AudioDecoder({
            output: (a) => this._onDecoded(a),
            error: (e) => console.error('AudioDecoder error:', e),
        });
        this.decoder.configure({
            codec: 'opus', sampleRate: AUDIO_SAMPLE_RATE, numberOfChannels: channels,
        });
        this.numChannels = channels;
    }

    _onDecoded(audioData) {
        if (this.stopped) { audioData.close(); return; }
        const numCh = audioData.numberOfChannels;
        const numFrames = audioData.numberOfFrames;
        if (!this._loggedFirstFrame) {
            this._loggedFirstFrame = true;
            console.log(
                `first decoded frame ${this.freqHz/1e6} MHz: format=${audioData.format} ` +
                `rate=${audioData.sampleRate} ch=${numCh} frames=${numFrames}`
            );
        }
        const buffer = this.audioContext.createBuffer(numCh, numFrames, audioData.sampleRate);
        // Ask WebCodecs for planar f32 regardless of the decoder's native layout;
        // both Chrome and Firefox perform the conversion. This avoids the manual
        // de-interleave path (and its off-by-ones) entirely.
        for (let ch = 0; ch < numCh; ch++) {
            audioData.copyTo(buffer.getChannelData(ch), { planeIndex: ch, format: 'f32-planar' });
        }
        audioData.close();

        this.pending.push(buffer);
        if (!this.playing) {
            if (this.pending.length >= MIN_BUFFER_PACKETS) {
                this.playing = true;
                this.nextPlayTime = this.audioContext.currentTime + START_DELAY_SEC;
                this._drain();
            }
            return;
        }
        this._drain();
    }

    _drain() {
        while (this.pending.length > 0) {
            const buf = this.pending.shift();
            const src = this.audioContext.createBufferSource();
            src.buffer = buf;
            src.connect(this.audioContext.destination);
            const now = this.audioContext.currentTime;
            if (this.nextPlayTime < now) {
                console.warn(`audio underrun ${this.freqHz/1e6} MHz: ${((now - this.nextPlayTime)*1000).toFixed(0)} ms late`);
                this.nextPlayTime = now + 0.02;
            }
            src.start(this.nextPlayTime);
            this.nextPlayTime += buf.duration;
        }
    }

    stop() {
        this.stopped = true;
        if (this.ws)      { try { this.ws.close(); } catch (_) {} this.ws = null; }
        if (this.decoder && this.decoder.state !== 'closed') {
            try { this.decoder.close(); } catch (_) {}
        }
        this.decoder = null;
        if (this.audioContext) { try { this.audioContext.close(); } catch (_) {} this.audioContext = null; }
        this.pending = [];
        this.playing = false;
    }
}

let audioSession = null;

async function listenToStation(freqHz) {
    audioPanel.classList.remove('hidden');
    resumeAudioBtn.classList.add('hidden');
    if (!audioSession) {
        audioSession = new AudioSession();
        try {
            await audioSession.start();
        } catch (err) {
            console.error('Audio start failed:', err);
            alert(`Failed to start audio: ${err.message}`);
            audioSession = null;
            return;
        }
    }
    // No optimistic callsign here -- it's set from the "tuned" confirmation
    // (by freq_hz, not by whatever was clicked), so it never has to be
    // corrected out from under a rapid second click.
    setAudioStatus('tuning…');
    await audioSession.tune(freqHz);
    // openPopup() already replaces whatever popup is open, so there is
    // nothing to close first -- a compensating map.closePopup() here would
    // run (this function is async) after any caller's own re-open of this
    // same marker and just close what was meant to stay open.
    const marker = markers[freqHz];
    if (marker) marker.openPopup();
}
window.listenToStation = listenToStation;  // popup onclick

// ---------------------------------------------------------------------------
// Frequency strip — stations by frequency, and where the receiver's window is.
// The map answers "who is near me"; this answers "what can the radio hear at
// once". The window box is drawn only from a measured position (see the
// "window" message); it is never inferred, because radiod places the front end
// on its own terms.
// ---------------------------------------------------------------------------
let currentWindow = null;   // {low_hz, high_hz} or null

function stripBounds() {
    if (!stationsData.length) return null;
    const fs = stationsData.map(s => s.freq_hz);
    let lo = Math.min(...fs), hi = Math.max(...fs);
    const pad = Math.max((hi - lo) * 0.05, 200e3);
    return { lo: lo - pad, hi: hi + pad };
}

// Pick a tick step from a 1-2-5 sequence scaled by powers of ten (100 kHz,
// 200 kHz, 500 kHz, 1 MHz, 2 MHz, 5 MHz, ...) so roughly 6-10 labels appear
// regardless of span — this has to read sensibly for both a 20 MHz FM span
// and a 150 kHz NWS span.
const TICK_TARGET_COUNT = 8;

function niceTickStepHz(spanHz) {
    const target = spanHz / TICK_TARGET_COUNT;
    const magnitude = Math.pow(10, Math.floor(Math.log10(target)));
    const residual = target / magnitude;
    let niceResidual;
    if (residual < 1.5) niceResidual = 1;
    else if (residual < 3.5) niceResidual = 2;
    else if (residual < 7.5) niceResidual = 5;
    else niceResidual = 10;
    return niceResidual * magnitude;
}

// Decimal places needed to show a step of this size in MHz without losing
// precision (0.1 MHz steps need one decimal, 0.05 MHz steps need two, ...).
function tickDecimals(stepHz) {
    const stepMhz = stepHz / 1e6;
    return Math.max(0, -Math.floor(Math.log10(stepMhz) + 1e-9));
}

function drawFreqStrip() {
    const canvas = document.getElementById('freq-strip-canvas');
    const caption = document.getElementById('freq-strip-caption');
    if (!canvas) return;
    const b = stripBounds();
    canvas.width = canvas.parentElement.clientWidth;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!b) { caption.textContent = ''; return; }
    const x = hz => ((hz - b.lo) / (b.hi - b.lo)) * canvas.width;
    const axisY = canvas.height - 18;

    // Window box first, so ticks draw over it.
    if (currentWindow) {
        const x0 = Math.max(0, x(currentWindow.low_hz));
        const x1 = Math.min(canvas.width, x(currentWindow.high_hz));
        if (x1 > x0) {
            ctx.fillStyle = 'rgba(59,130,246,0.18)';
            ctx.fillRect(x0, 0, x1 - x0, axisY);
            ctx.strokeStyle = 'rgba(59,130,246,0.7)';
            ctx.strokeRect(x0, 0, x1 - x0, axisY);
        }
    }

    ctx.strokeStyle = '#334155';
    ctx.beginPath();
    ctx.moveTo(0, axisY);
    ctx.lineTo(canvas.width, axisY);
    ctx.stroke();

    // Axis ticks + labels — subordinate to the station ticks: shorter marks,
    // muted color, drawn first so station ticks sit visually on top.
    const step = niceTickStepHz(b.hi - b.lo);
    const decimals = tickDecimals(step);
    ctx.fillStyle = '#64748b';
    ctx.font = '9px sans-serif';
    ctx.strokeStyle = '#475569';
    ctx.lineWidth = 1;
    const firstTick = Math.ceil(b.lo / step) * step;
    for (let hz = firstTick; hz <= b.hi; hz += step) {
        const tx = x(hz);
        ctx.beginPath();
        ctx.moveTo(tx, axisY);
        ctx.lineTo(tx, axisY + 4);
        ctx.stroke();
        const label = (hz / 1e6).toFixed(decimals);
        const w = ctx.measureText(label).width;
        const lx = Math.min(Math.max(tx - w / 2, 0), canvas.width - w);
        ctx.fillText(label, lx, canvas.height - 4);
    }

    for (const st of stationsData) {
        const sx = x(st.freq_hz);
        const inWindow = currentWindow &&
            st.freq_hz >= currentWindow.low_hz && st.freq_hz <= currentWindow.high_hz;
        ctx.strokeStyle = inWindow ? '#22c55e' : '#94a3b8';
        ctx.lineWidth = inWindow ? 2 : 1;
        ctx.beginPath();
        ctx.moveTo(sx, 6);
        ctx.lineTo(sx, axisY);
        ctx.stroke();
    }

    caption.textContent = currentWindow
        ? `receiver window ${((currentWindow.high_hz - currentWindow.low_hz) / 1e3).toFixed(0)} kHz`
        : (currentActivityAvailable ? '' : 'wider than the receiver — pick a station to listen');
}

function freqStripClick(ev) {
    const b = stripBounds();
    if (!b || !stationsData.length) return;
    const canvas = document.getElementById('freq-strip-canvas');
    const rect = canvas.getBoundingClientRect();
    const hz = b.lo + ((ev.clientX - rect.left) / rect.width) * (b.hi - b.lo);
    let best = stationsData[0];
    for (const st of stationsData) {
        if (Math.abs(st.freq_hz - hz) < Math.abs(best.freq_hz - hz)) best = st;
    }
    // listenToStation() already opens this marker's popup once the station
    // is selected; no compensating re-open needed here.
    listenToStation(best.freq_hz);
}

// Nearest-tick hover: a floating label near the cursor, not the canvas
// `title` attribute (too slow/coarse to update per pixel).
const FREQ_STRIP_HOVER_PX = 6;

function freqStripMouseMove(ev) {
    const hoverEl = document.getElementById('freq-strip-hover');
    if (!hoverEl) return;
    const b = stripBounds();
    if (!b || !stationsData.length) { hoverEl.classList.add('hidden'); return; }
    const canvas = document.getElementById('freq-strip-canvas');
    const container = canvas.parentElement;
    const canvasRect = canvas.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    const mx = ev.clientX - canvasRect.left;
    const x = hz => ((hz - b.lo) / (b.hi - b.lo)) * canvas.width;

    let best = null, bestDist = Infinity;
    for (const st of stationsData) {
        const dist = Math.abs(x(st.freq_hz) - mx);
        if (dist < bestDist) { bestDist = dist; best = st; }
    }

    if (!best || bestDist > FREQ_STRIP_HOVER_PX) {
        hoverEl.classList.add('hidden');
        return;
    }

    hoverEl.textContent = `${best.name} ${(best.freq_hz / 1e6).toFixed(3)}`;
    hoverEl.classList.remove('hidden');
    // Position relative to the container (which is the positioning parent),
    // offset from the cursor, clamped so the label can't run off either edge.
    let left = ev.clientX - containerRect.left + 10;
    const maxLeft = containerRect.width - hoverEl.offsetWidth - 4;
    left = Math.min(Math.max(left, 4), Math.max(maxLeft, 4));
    let top = ev.clientY - containerRect.top - 26;
    top = Math.max(top, 4);
    hoverEl.style.left = `${left}px`;
    hoverEl.style.top = `${top}px`;
}

function freqStripMouseLeave() {
    const hoverEl = document.getElementById('freq-strip-hover');
    if (hoverEl) hoverEl.classList.add('hidden');
}

stopAudioBtn.onclick = () => {
    if (audioSession) { audioSession.stop(); audioSession = null; }
    audioPanel.classList.add('hidden');
    audioRepeaterCallsign.textContent = '—';
    audioRepeaterFreq.textContent = '—';
};

resumeAudioBtn.addEventListener('click', async () => {
    if (audioSession && audioSession.audioContext) {
        try {
            await audioSession.audioContext.resume();
            resumeAudioBtn.classList.add('hidden');
        } catch (e) { console.error('Audio manual resume failed:', e); }
    }
});

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------
hostRefreshBtn.addEventListener('click', () => discoverHosts());
hostSelect.addEventListener('change', (e) => selectHost(e.target.value));

modeSelect.addEventListener('change', (e) => {
    currentMode = e.target.value;
    localStorage.setItem('radiod-monitor.mode', currentMode);
    renderModeControls();
    triggerSearch();
});

searchBtn.addEventListener('click', triggerSearch);
locationInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') triggerSearch(); });

squelchSlider.addEventListener('input',  (e) => { squelchValue.textContent = e.target.value; });
squelchSlider.addEventListener('change', () => triggerSearch());
radiusSlider.addEventListener('input',  (e) => { radiusValue.textContent = e.target.value; });
radiusSlider.addEventListener('change', () => triggerSearch());

const freqStripCanvasEl = document.getElementById('freq-strip-canvas');
freqStripCanvasEl.addEventListener('click', freqStripClick);
freqStripCanvasEl.addEventListener('mousemove', freqStripMouseMove);
freqStripCanvasEl.addEventListener('mouseleave', freqStripMouseLeave);
window.addEventListener('resize', drawFreqStrip);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
(async function boot() {
    initMap();
    await discoverHosts();
    await loadSources();
    connectControl();
})();
