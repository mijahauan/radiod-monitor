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
let currentAudioChannels = 1;   // set from each results message
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
        if (currentSession) { currentSession.stop(); currentSession = null; }
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
}

// ---------------------------------------------------------------------------
// Search results + activity
// ---------------------------------------------------------------------------
function handleSearchResults(data) {
    stationsData = data.stations || [];
    repeaterCount.textContent = stationsData.length;
    // Remember the channel count the backend is emitting for this mode so
    // listenToStation can configure the WebCodecs AudioDecoder correctly.
    // wfm is stereo (2 channels), everything else is mono.
    currentAudioChannels = data.audio_channels || 1;
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
    btn.addEventListener('click', () => listenToStation(st.freq_hz, st.name));
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
// Audio — WebSocket + WebCodecs AudioDecoder
// ---------------------------------------------------------------------------
const AUDIO_SAMPLE_RATE = 48000;
const MIN_BUFFER_PACKETS = 5;   // ~100 ms jitter buffer
const START_DELAY_SEC = 0.15;

class AudioSession {
    constructor(freqHz, numChannels) {
        this.freqHz = freqHz;
        this.numChannels = numChannels || 1;
        this.ws = null;
        this.audioContext = null;
        this.decoder = null;
        this.pending = [];
        this.nextPlayTime = 0;
        this.playing = false;
        this.stopped = false;
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
        this._rxCount = 0;
        this._decodeErrCount = 0;
        this.decoder = new AudioDecoder({
            output: (audioData) => this._onDecoded(audioData),
            error: (e) => console.error('AudioDecoder error callback:', e),
        });
        // Configuration waits for the server's config message, which carries
        // the channel count read from the first Opus frame's TOC byte. A
        // count guessed from the preset is not reliable — the wfm preset
        // ships mono in some installs and stereo in others — and a wrong
        // numberOfChannels makes WebCodecs throw on the first packet.

        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${proto}//${window.location.host}/ws/audio/${this.freqHz}`);
        this.ws.binaryType = 'arraybuffer';
        this.ws.onopen    = () => console.log(`Audio WS open for ${this.freqHz/1e6} MHz`);
        this.ws.onerror   = (e) => console.warn('Audio WS error', e);
        this.ws.onclose   = () => console.log(`Audio WS closed for ${this.freqHz/1e6} MHz`);
        this.ws.onmessage = (ev) => {
            if (this.stopped) return;
            if (typeof ev.data === 'string') {
                this._onControlMessage(ev.data);
                return;
            }
            if (!(ev.data instanceof ArrayBuffer)) return;
            if (!this.decoder || this.decoder.state !== 'configured') {
                // Frames that arrive before the config message have no
                // decoder to go to. Opus frames are self-contained, so
                // dropping the first few costs at most a few ms of audio.
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

    _onControlMessage(text) {
        let msg;
        try { msg = JSON.parse(text); } catch (_) { return; }
        if (msg.type !== 'config' || !msg.channels) return;
        if (!this.decoder || this.decoder.state === 'closed') return;
        if (this.decoder.state === 'configured' && this.numChannels === msg.channels) return;
        this.numChannels = msg.channels;
        console.log(`${this.freqHz/1e6} MHz: configuring decoder for ${msg.channels}-channel Opus`);
        try {
            this.decoder.configure({
                codec: 'opus',
                sampleRate: AUDIO_SAMPLE_RATE,
                numberOfChannels: msg.channels,
            });
        } catch (e) {
            console.error('AudioDecoder configure failed:', e);
        }
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

let currentSession = null;

function listenToStation(freqHz, name) {
    if (currentSession) { currentSession.stop(); currentSession = null; }
    audioRepeaterCallsign.textContent = name;
    audioRepeaterFreq.textContent = `${(freqHz / 1e6).toFixed(3)} MHz`;
    audioPanel.classList.remove('hidden');
    resumeAudioBtn.classList.add('hidden');

    const session = new AudioSession(freqHz, currentAudioChannels);
    currentSession = session;
    session.start().catch(err => {
        console.error('Audio start failed:', err);
        alert(`Failed to start audio: ${err.message}`);
        session.stop();
        if (currentSession === session) currentSession = null;
    });
    map.closePopup();
}
window.listenToStation = listenToStation;  // popup onclick

stopAudioBtn.onclick = () => {
    if (currentSession) { currentSession.stop(); currentSession = null; }
    audioPanel.classList.add('hidden');
    audioRepeaterCallsign.textContent = '—';
};

resumeAudioBtn.addEventListener('click', async () => {
    if (currentSession && currentSession.audioContext) {
        try {
            await currentSession.audioContext.resume();
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

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
(async function boot() {
    initMap();
    await discoverHosts();
    await loadSources();
    connectControl();
})();
