"""X32 OSC client — read-only in Phase 1.

Connection strategy:
  1. Bind a single UDP socket to 0.0.0.0:listen_port (default 10024).
     This socket is used for BOTH sending and receiving so the X32 always
     sees our source port as listen_port and sends replies back there.
     Using a separate SimpleUDPClient would send from an ephemeral OS port
     and the reply would never reach the ThreadingOSCUDPServer.
  2. Send /xremote to register for push updates (all param changes pushed to us)
  3. Use /node bulk reads to snapshot all 14 channels at startup
  4. Subscribe to /meters/1 via /batchsubscribe for 50ms RMS pushes
  5. Keepalive thread renews /xremote and /renew every 8 seconds
"""

import math
import socket as _socket
import struct
import threading
import time
from typing import Callable, Optional

import numpy as np
from pythonosc import dispatcher, osc_server
from pythonosc.osc_message_builder import OscMessageBuilder

from models.channel import ChannelState, ChannelConfig, ChannelMeterState, EQBand


# Compressor ratio enum index → actual ratio
COMP_RATIO_MAP = {0: 1.1, 1: 1.3, 2: 1.5, 3: 2.0, 4: 2.5, 5: 3.0,
                  6: 4.0, 7: 5.0, 8: 7.0, 9: 10.0, 10: 20.0, 11: 100.0}

METERS_ALIAS = "/foh_meters"
RTA_ALIAS      = "/foh_rta"

RTA_FREQS_HZ = [
    20, 21, 22, 24, 26, 28, 30, 32, 34, 36, 39, 42, 45, 48, 52, 55, 59,
    63, 68, 73, 78, 84, 90, 96, 103, 110, 118, 127, 136, 146, 156, 167,
    179, 192, 206, 221, 237, 254, 272, 292, 313, 335, 359, 385, 412, 442,
    474, 508, 544, 583, 625, 670, 718, 769, 825, 884, 947, 1020, 1090,
    1170, 1250, 1340, 1440, 1540, 1650, 1770, 1890, 2030, 2180, 2330,
    2500, 2680, 2870, 3080, 3300, 3540, 3790, 4060, 4350, 4670, 5000,
    5360, 5740, 6160, 6600, 7070, 7580, 8120, 8710, 9330, 10000, 10720,
    11490, 12310, 13200, 14140, 15160, 16250, 17410, 18660
]

KEEPALIVE_INTERVAL = 8.0        # seconds — X32 times out after 10s

# RTA source index constants (/-action/setrtasrc)
MAIN_LR_RTA_INDEX = 70          # Main L/R — default always-on monitoring position


def channel_to_rta_index(channel_num: int) -> int:
    """Convert 1-based channel number to post-EQ setrtasrc index.

    Ch01 post-EQ = 98, Ch32 post-EQ = 129.
    Main L/R = 70 (use as default/return position).
    """
    return (channel_num - 1) + 98


# ---------------------------------------------------------------------------
# Fader and EQ conversions (X32 OSC Reference, Section 9 & 10)
# ---------------------------------------------------------------------------

def fader_float_to_db(f: float) -> float:
    """Convert X32 fader float [0.0, 1.0] to dB [-90, +10].
    4-segment piecewise linear approximation per protocol spec."""
    if f >= 0.5:
        return f * 40.0 - 30.0       # -10 to +10 dB
    elif f >= 0.25:
        return f * 80.0 - 50.0       # -30 to -10 dB
    elif f >= 0.0625:
        return f * 160.0 - 70.0      # -60 to -30 dB
    elif f > 0.0:
        return f * 480.0 - 90.0      # -90 to -60 dB
    else:
        return -90.0


def db_to_fader_float(d: float) -> float:
    """Convert dB [-90, +10] to X32 fader float [0.0, 1.0]."""
    if d < -60.0:
        f = (d + 90.0) / 480.0
    elif d < -30.0:
        f = (d + 70.0) / 160.0
    elif d < -10.0:
        f = (d + 50.0) / 80.0
    else:
        f = (d + 30.0) / 40.0
    return int(f * 1023.5) / 1023.0


def eq_float_to_hz(f: float) -> float:
    """Convert X32 EQ freq float [0.0, 1.0] to Hz [20, 20000] (log scale)."""
    log_min = math.log10(20.0)
    log_max = math.log10(20000.0)
    return 10 ** (log_min + f * (log_max - log_min))


def linear_to_dbfs(linear: float) -> float:
    """Convert X32 meter float [0.0-1.0] to dBFS."""
    if linear <= 0:
        return -90.0
    return max(-90.0, 20 * math.log10(linear))


def parse_meters_1(blob_data: bytes) -> dict:
    """Parse /meters/1 blob. Returns per-channel RMS as linear floats."""
    # blob: 4B big-endian length + 4B little-endian count + little-endian floats
    num_floats = struct.unpack_from("<I", blob_data, 4)[0]
    floats = struct.unpack_from(f"<{num_floats}f", blob_data, 8)
    return {
        "channel_rms":  list(floats[0:32]),
        "gate_gr":      list(floats[32:64]),
        "dynamics_gr":  list(floats[64:96]),
    }


def parse_meters_15(blob_data: bytes) -> np.ndarray:
    """Parse /meters/15 RTA blob. Returns 100-band dBFS array."""
    num_ints   = struct.unpack_from("<I", blob_data, 4)[0]
    raw_shorts = struct.unpack_from(f"<{num_ints}h", blob_data, 8)
    return np.array([s / 256.0 for s in raw_shorts[:100]])


def parse_meters_6(blob_data: bytes) -> dict:
    """Parse /meters/6 single-channel strip meter."""
    num_floats = struct.unpack_from("<I", blob_data, 4)[0]
    floats = struct.unpack_from(f"<{num_floats}f", blob_data, 8)
    return {
        "pre_fade":  floats[0] if len(floats) > 0 else 0.0,
        "gate_gr":   floats[1] if len(floats) > 1 else 1.0,
        "dyn_gr":    floats[2] if len(floats) > 2 else 1.0,
        "post_fade": floats[3] if len(floats) > 3 else 0.0,
    }


# ---------------------------------------------------------------------------
# Debug server — prints raw bytes for every received packet
# ---------------------------------------------------------------------------

class _DebugOSCUDPServer(osc_server.ThreadingOSCUDPServer):
    """Instruments every incoming UDP packet with a raw hex/text dump.

    Enable by passing debug=True to X32OSCClient.__init__. Useful when
    Wireshark shows packets arriving but the app appears not to see them.
    """
    def process_request(self, request, client_address):
        data, _ = request
        hex_preview = data[:80].hex()
        text_preview = data[:80].decode("ascii", errors="replace").replace("\x00", "·")
        print(f"[OSC DEBUG] {len(data)}B from {client_address[0]}:{client_address[1]}")
        print(f"  hex:  {hex_preview}")
        print(f"  text: {text_preview!r}")
        super().process_request(request, client_address)


# ---------------------------------------------------------------------------
# OSC Client
# ---------------------------------------------------------------------------

class X32OSCClient:
    def __init__(self, ip: str, port: int, channel_map: dict,
                 listen_port: int = 10024, debug: bool = False,
                 poll_interval_ms: int = 500):
        self._ip = ip
        self._port = port
        self._channel_map = channel_map   # {num: {label, type, ...}}
        self._listen_port = listen_port
        self._debug = debug
        self._poll_interval_s: float = poll_interval_ms / 1000.0

        # Per-channel last-received timestamp (push or /node response).
        # The poll fallback re-requests /node for channels silent > stale threshold.
        self._last_push_time: dict[int, float] = {}

        # Bound UDP socket — set by _start_listener().
        # Used for BOTH outbound sends and inbound receives so the X32 always
        # replies to listen_port rather than a random ephemeral source port.
        self._send_sock: Optional[_socket.socket] = None

        # Shared state — written by OSC handler thread, read by main thread
        self._state: dict = {}              # raw param values per channel + "_info"
        self._meter_rms: list[float] = [0.0] * 32
        self._state_lock = threading.Lock()

        self._server: Optional[osc_server.ThreadingOSCUDPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False

        self._on_adjustment: Optional[Callable] = None  # callback for detected changes

        # Phase 2 state
        self._board_rta_db: Optional[np.ndarray] = None
        self._channel_configs: dict[int, ChannelConfig] = {}
        self._channel_meters: dict[int, ChannelMeterState] = {}
        self._config_dirty: set[int] = set()
        self._meter_gate_gr: list[float] = [1.0] * 32
        self._meter_dyn_gr:  list[float] = [1.0] * 32
        self._on_config_change = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 5.0) -> str:
        """Start listener, send /xremote, verify with /info. Returns console info string."""
        self._running = True
        self._start_listener()
        self._send("/xremote", [])
        info = self._request_info(timeout)
        self._start_keepalive()
        self._start_poll_fallback()
        self._subscribe_meters()
        return info

    def close(self) -> None:
        self._running = False
        try:
            self._send("/unsubscribe", [METERS_ALIAS])
            self._send("/unsubscribe", [RTA_ALIAS])
        except Exception:
            pass
        if self._server:
            self._server.shutdown()
            self._server.server_close()  # releases the bound socket
        if self._server_thread:
            self._server_thread.join(timeout=2.0)
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=2.0)
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot_all_channels(self) -> dict[int, ChannelState]:
        """Read full state for all mapped channels.

        Sends both /node bulk requests (real X32 / Python sim) AND individual
        parameter queries (Maillot emulator and other /node-less implementations).
        Whichever responds first populates the state.
        """
        for ch_num in self._channel_map:
            ch = f"{ch_num:02d}"
            # Bulk read — real X32 and Python simulator
            self._send("/node", [f"ch/{ch}/mix"])
            self._send("/node", [f"ch/{ch}/eq"])
            self._send("/node", [f"ch/{ch}/dyn"])
            self._send("/node", [f"ch/{ch}/gate"])
            self._send("/node", [f"ch/{ch}/preamp"])
            self._send("/node", [f"ch/{ch}/config"])
            # Individual param queries — Maillot emulator fallback.
            # Paced one channel at a time to avoid overwhelming emulator response queue.
            for addr in [
                f"ch/{ch}/mix/fader",      f"ch/{ch}/mix/on",
                f"ch/{ch}/eq/on",
                f"ch/{ch}/eq/1/f",  f"ch/{ch}/eq/1/g",
                f"ch/{ch}/eq/2/f",  f"ch/{ch}/eq/2/g",
                f"ch/{ch}/eq/3/f",  f"ch/{ch}/eq/3/g",
                f"ch/{ch}/eq/4/f",  f"ch/{ch}/eq/4/g",
                f"ch/{ch}/preamp/hpf",     f"ch/{ch}/preamp/hpslope",
                f"ch/{ch}/dyn/on",         f"ch/{ch}/gate/on",
            ]:
                self._send(f"/{addr}", [])
            time.sleep(0.02)   # 20ms per channel lets emulator process & respond
        time.sleep(0.3)   # final settle
        return self.build_channel_states()

    def build_channel_states(self) -> dict[int, ChannelState]:
        """Build ChannelState objects from current cached raw state."""
        now = time.time()
        result: dict[int, ChannelState] = {}
        with self._state_lock:
            rms_snapshot = list(self._meter_rms)
            state_snapshot = {k: v.copy() for k, v in self._state.items()
                              if isinstance(k, int)}

        for ch_num, ch_cfg in self._channel_map.items():
            raw = state_snapshot.get(ch_num, {})
            yaml_label = ch_cfg.get("label", f"CH{ch_num:02d}")
            x32_name = raw.get("x32_name", "").strip()
            label = yaml_label    # canonical identifier; x32_name stored separately for display
            rms_linear = rms_snapshot[ch_num - 1] if ch_num <= 32 else 0.0

            eq_bands = []
            for b in range(1, 5):
                eq_bands.append(EQBand(
                    band_num=b,
                    type=raw.get(f"eq_{b}_type", 2),
                    freq_hz=eq_float_to_hz(raw.get(f"eq_{b}_freq", 0.5)),
                    gain_db=raw.get(f"eq_{b}_gain", 0.0),   # reads directly in dB
                    q=raw.get(f"eq_{b}_q", 1.0),
                ))

            fader_f = raw.get("fader", 0.75)
            mute_val = raw.get("mute", 1)       # 1 = ON = unmuted per X32 spec

            result[ch_num] = ChannelState(
                channel_num=ch_num,
                label=label,
                fader_db=fader_float_to_db(fader_f),
                muted=(mute_val == 0),           # 0=OFF=muted
                eq=eq_bands,
                comp_on=bool(raw.get("comp_on", 0)),
                comp_threshold_db=raw.get("comp_thr", -20.0),
                comp_ratio_index=raw.get("comp_ratio", 3),
                gate_on=bool(raw.get("gate_on", 0)),
                gate_threshold_db=raw.get("gate_thr", -40.0),
                rms_linear=rms_linear,
                rms_db=linear_to_dbfs(rms_linear),
                timestamp=now,
                channel_type=ch_cfg.get("type", "instrument"),
                usage=ch_cfg.get("usage"),
                inactive_threshold_db=ch_cfg.get("inactive_threshold_db"),
                paired_channel=ch_cfg.get("paired_channel"),
                role=ch_cfg.get("role"),
                priority=ch_cfg.get("priority"),
                hpf_on=eq_float_to_hz(raw.get("preamp_hpf", 0.0)) > 22.0,
                hpf_freq_hz=eq_float_to_hz(raw.get("preamp_hpf", 0.3)),
                hpf_slope=raw.get("preamp_hpslope", 1),
                input_gain_db=raw.get("preamp_gain", 0.0),
                x32_name=raw.get("x32_name", ""),
            )
        return result

    def build_channel_configs(self) -> dict:
        """Build ChannelConfig objects from current cached raw state."""
        from core.channel_model import compute_transfer_curves, hpslope_int_to_db_oct, COMP_RATIO_MAP as _RATIO_MAP
        import time as _time
        now = _time.time()
        result = {}
        with self._state_lock:
            state_snap = {k: v.copy() for k, v in self._state.items() if isinstance(k, int)}
        for ch_num, ch_cfg in self._channel_map.items():
            raw = state_snap.get(ch_num, {})
            label = ch_cfg.get('label', f'CH{ch_num:02d}') if isinstance(ch_cfg, dict) else str(ch_cfg)
            instr_type = ch_cfg.get('instrument_type', 'guitar') if isinstance(ch_cfg, dict) else 'guitar'
            hpf_freq_hz = eq_float_to_hz(raw.get('preamp_hpf', 0.0))
            hpf_enabled = hpf_freq_hz > 22.0
            fader_db = fader_float_to_db(raw.get('fader', 0.75))
            eq_enabled = bool(raw.get('eq_on', 1))
            eq_bands_list = []
            for b in range(1, 5):
                eq_bands_list.append(EQBand(
                    band_num=b, type=raw.get(f'eq_{b}_type', 2),
                    freq_hz=eq_float_to_hz(raw.get(f'eq_{b}_freq', 0.5)),
                    gain_db=raw.get(f'eq_{b}_gain', 0.0),
                    q=max(raw.get(f'eq_{b}_q', 1.0), 0.1),
                ))
            comp_ratio_idx = raw.get('comp_ratio', 3)
            comp_ratio = _RATIO_MAP.get(comp_ratio_idx, 2.0)
            cfg = ChannelConfig(
                channel_num=ch_num, label=label, instrument_type=instr_type,
                trim_db=raw.get('preamp_gain', 0.0),
                polarity_inverted=bool(raw.get('preamp_invert', 0)),
                hpf_enabled=hpf_enabled, hpf_freq_hz=hpf_freq_hz,
                hpf_slope_db_oct=hpslope_int_to_db_oct(raw.get('preamp_hpslope', 0)),
                eq_enabled=eq_enabled, eq_bands=eq_bands_list,
                fader_db=fader_db, muted=(raw.get('mute', 1) == 0), pan=raw.get('mix_pan', 0.0),
                comp_enabled=bool(raw.get('comp_on', 0)), comp_threshold_db=raw.get('comp_thr', -20.0),
                comp_ratio=comp_ratio, comp_attack_ms=raw.get('comp_attack', 10.0),
                comp_release_ms=raw.get('comp_release', 100.0), comp_makeup_db=raw.get('comp_mgain', 0.0),
                gate_enabled=bool(raw.get('gate_on', 0)), gate_threshold_db=raw.get('gate_thr', -40.0),
                gate_range_db=raw.get('gate_range', 20.0), last_config_update=now,
            )
            compute_transfer_curves(cfg)
            result[ch_num] = cfg
        with self._state_lock:
            self._channel_configs = result
            self._config_dirty.clear()
        return result

    def update_dirty_configs(self) -> set:
        """Recompute transfer curves for dirty channels."""
        with self._state_lock:
            dirty = set(self._config_dirty)
            self._config_dirty.clear()
        if not dirty:
            return set()
        self.build_channel_configs()
        return dirty

    def request_meters_6(self, channel_id_0based: int) -> None:
        self._send('/meters', [f'/meters/6', channel_id_0based])

    @property
    def board_rta_db(self) -> 'np.ndarray':
        import numpy as np
        with self._state_lock:
            if self._board_rta_db is not None:
                return self._board_rta_db.copy()
        return np.full(100, -90.0)

    @property
    def channel_configs(self) -> dict:
        with self._state_lock:
            return dict(self._channel_configs)

    @property
    def channel_meters(self) -> dict:
        with self._state_lock:
            return dict(self._channel_meters)

    def set_rta_source(self, source_index: int) -> None:
        """Switch the X32 RTA analyzer (/meters/15) to a specific source.

        source_index: 0–31 = Ch01–32 pre-EQ, 98–129 = Ch01–32 post-EQ,
                      70 = Main L/R (default monitoring position).
        Always call set_rta_source(MAIN_LR_RTA_INDEX) after investigations.
        """
        self._send("/-action/setrtasrc", [source_index])

    def set_rta_position(self, post_eq: bool = True) -> None:
        """Set RTA tap point to pre-EQ (0) or post-EQ (1). Always use post-EQ."""
        self._send("/-prefs/rta/pos", [1 if post_eq else 0])

    def read_main_fader(self) -> float:
        """Return current main LR fader in dB."""
        self._send("/main/st/mix/fader", [])
        time.sleep(0.1)
        with self._state_lock:
            return fader_float_to_db(self._state.get(0, {}).get("main_fader", 0.75))

    # ------------------------------------------------------------------
    # Internal — send helper
    # ------------------------------------------------------------------

    def _send(self, address: str, params=None) -> None:
        """Build an OSC message and send it from the bound listen socket.

        Sending from self._send_sock (which is the ThreadingOSCUDPServer's
        socket bound to 0.0.0.0:listen_port) ensures the X32 sees our source
        port as listen_port and sends all replies back there.
        """
        if self._send_sock is None:
            return
        builder = OscMessageBuilder(address=address)
        for p in (params or []):
            builder.add_arg(p)
        msg = builder.build()
        self._send_sock.sendto(msg.dgram, (self._ip, self._port))

    # ------------------------------------------------------------------
    # Internal — listener
    # ------------------------------------------------------------------

    def _start_listener(self) -> None:
        disp = dispatcher.Dispatcher()
        disp.map("/node", self._handle_node)
        disp.map("/ch/*", self._handle_channel_param)
        disp.map("/main/*", self._handle_main_param)
        disp.map(METERS_ALIAS, self._handle_meters)
        disp.map(RTA_ALIAS, self._handle_rta)
        disp.set_default_handler(self._handle_default)

        server_class = _DebugOSCUDPServer if self._debug else osc_server.ThreadingOSCUDPServer
        self._server = server_class(("0.0.0.0", self._listen_port), disp)

        # Reuse the server's already-bound socket for outbound sends.
        # The socket is bound synchronously in ThreadingOSCUDPServer.__init__,
        # before serve_forever is called, so it's safe to use immediately.
        self._send_sock = self._server.socket

        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._server_thread.start()

        # Give serve_forever time to enter its select() loop before we send
        # the first packet. Without this, a very fast emulator reply could
        # arrive in the OS buffer before serve_forever starts draining it —
        # harmless on most systems but avoids a subtle race on loaded hosts.
        time.sleep(0.05)

    def _start_keepalive(self) -> None:
        def loop():
            while self._running:
                time.sleep(KEEPALIVE_INTERVAL)
                if not self._running:
                    break
                self._send("/xremote", [])
                self._send("/renew", [METERS_ALIAS])
                self._send("/renew", [RTA_ALIAS])

        self._keepalive_thread = threading.Thread(target=loop, daemon=True)
        self._keepalive_thread.start()

    def _is_channel_stale(self, ch_num: int, now: float,
                          stale_s: float = 2.0) -> bool:
        """True if ch_num has not received any update within stale_s seconds."""
        return (now - self._last_push_time.get(ch_num, 0.0)) > stale_s

    def _start_poll_fallback(self) -> None:
        """Background thread that re-requests /node for channels that have gone
        silent — no push update received within stale_s seconds.

        Push updates via /xremote are unreliable over venue WiFi.  This fallback
        keeps board state current even when push packets are dropped.
        Stale threshold = max(2.0 s, 4 × poll_interval) — at least four cycles.
        """
        stale_s = max(2.0, self._poll_interval_s * 4)

        def loop():
            while self._running:
                time.sleep(self._poll_interval_s)
                if not self._running:
                    break
                now = time.time()
                for ch_num in self._channel_map:
                    if self._is_channel_stale(ch_num, now, stale_s):
                        ch = f"{ch_num:02d}"
                        self._send("/node", [f"ch/{ch}/mix"])
                        self._send("/node", [f"ch/{ch}/eq"])
                        # Individual queries for emulators without /node support
                        self._send(f"/ch/{ch}/mix/fader", [])
                        self._send(f"/ch/{ch}/mix/on", [])
                        self._send(f"/ch/{ch}/eq/on", [])

        self._poll_thread = threading.Thread(target=loop, daemon=True)
        self._poll_thread.start()

    def _subscribe_meters(self) -> None:
        # /batchsubscribe alias meter_cmd arg1 arg2 time_factor
        self._send("/batchsubscribe", [METERS_ALIAS, "/meters/1", 0, 0, 1])
        self._send("/batchsubscribe", [RTA_ALIAS,    "/meters/15", 1, 0, 1])

    def _request_info(self, timeout: float) -> str:
        """Send /info and wait for response. Returns info string."""
        with self._state_lock:
            self._state["_info"] = None
        self._send("/info", [])
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._state_lock:
                info = self._state.get("_info")
            if info is not None:
                return info
            time.sleep(0.05)    # tight poll — X32 responds in <10ms
        raise ConnectionError(
            f"No response from X32 at {self._ip}:{self._port} within {timeout}s. "
            "Check IP address and network connection."
        )

    # ------------------------------------------------------------------
    # OSC message handlers
    # ------------------------------------------------------------------

    def _handle_node(self, address: str, *args) -> None:
        # /node response: first arg is the node path, remaining are values
        if not args:
            return
        node_path = args[0]
        values = args[1:]
        # e.g. node_path = "ch/01/mix", values = (fader, on, pan, ...)
        parts = node_path.strip("/").split("/")
        if len(parts) < 2:
            return
        if parts[0] == "ch" and len(parts) >= 2:
            try:
                ch_num = int(parts[1])
            except ValueError:
                return
            section = parts[2] if len(parts) > 2 else ""
            with self._state_lock:
                raw = self._state.setdefault(ch_num, {})
                if section == "mix" and len(values) >= 2:
                    raw["fader"] = float(values[0])
                    raw["mute"] = int(values[1])
                    if len(values) >= 3:
                        raw["mix_pan"] = float(values[2])
                elif section == "eq":
                    # eq_on at index 0, then 4 bands: band1_type band1_f band1_g band1_q band2_...
                    raw["eq_on"] = int(values[0])
                    for b in range(4):
                        offset = 1 + b * 4
                        if offset + 3 < len(values):
                            raw[f"eq_{b+1}_type"] = int(values[offset])
                            raw[f"eq_{b+1}_freq"] = float(values[offset + 1])
                            raw[f"eq_{b+1}_gain"] = float(values[offset + 2])
                            raw[f"eq_{b+1}_q"] = float(values[offset + 3])
                elif section == "dyn" and len(values) >= 3:
                    raw["comp_on"] = int(values[0])
                    raw["comp_thr"] = float(values[1])
                    raw["comp_ratio"] = int(values[2])
                    if len(values) >= 7:
                        raw["comp_attack"]  = float(values[4])
                        raw["comp_release"] = float(values[5])
                        raw["comp_mgain"]   = float(values[6])
                elif section == "gate" and len(values) >= 2:
                    raw["gate_on"] = int(values[0])
                    raw["gate_thr"] = float(values[1])
                    if len(values) >= 3:
                        raw["gate_range"] = float(values[2])
                elif section == "preamp" and len(values) >= 5:
                    # X32 preamp node field order: gain, invert, hpon, hpf, hpslope, lofilt
                    raw["preamp_gain"] = float(values[0])
                    raw["preamp_invert"] = int(values[1])
                    raw["preamp_hpon"] = int(values[2])
                    raw["preamp_hpf"] = float(values[3])
                    raw["preamp_hpslope"] = int(values[4])
                elif section == "config" and values:
                    value_str = str(values[0])
                    parts_cfg = value_str.split()
                    if parts_cfg:
                        raw["x32_name"] = parts_cfg[0].strip('"')
            self._last_push_time[ch_num] = time.time()

    def _handle_channel_param(self, address: str, *args) -> None:
        # Individual parameter push from /xremote: /ch/01/mix/fader ,f value
        parts = address.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "ch":
            return
        try:
            ch_num = int(parts[1])
        except ValueError:
            return
        if not args:
            return
        param_path = "/".join(parts[2:])
        val = args[0]
        with self._state_lock:
            raw = self._state.setdefault(ch_num, {})
            if param_path == "mix/fader":
                raw["fader"] = float(val)
            elif param_path == "mix/on":
                raw["mute"] = int(val)
            elif len(parts) == 5 and parts[2] == "eq":
                b = int(parts[3])
                sub = parts[4]
                if sub == "g":
                    raw[f"eq_{b}_gain"] = float(val)
                elif sub == "f":
                    raw[f"eq_{b}_freq"] = float(val)
                elif sub == "q":
                    raw[f"eq_{b}_q"] = float(val)
                elif sub == "t":
                    raw[f"eq_{b}_type"] = int(val)
            elif param_path == "dyn/thr":
                raw["comp_thr"] = float(val)
            elif param_path == "dyn/on":
                raw["comp_on"] = int(val)
            elif param_path == "dyn/rat":
                raw["comp_ratio"] = int(val)
            elif param_path == "gate/thr":
                raw["gate_thr"] = float(val)
            elif param_path == "gate/on":
                raw["gate_on"] = int(val)
            elif param_path == "preamp/gain":
                raw["preamp_gain"] = float(val)
            elif param_path == "preamp/hpon":
                raw["preamp_hpon"] = int(val)
            elif param_path == "preamp/hpf":
                raw["preamp_hpf"] = float(val)
            elif param_path == "preamp/hpslope":
                raw["preamp_hpslope"] = int(val)
            elif param_path == "preamp/invert":
                raw["preamp_invert"] = int(val)
            elif param_path == "eq/on":
                raw["eq_on"] = int(val)
            is_config_param = parts[2] in ('eq', 'preamp') or param_path in ('mix/fader', 'mix/on')
            if is_config_param:
                self._config_dirty.add(ch_num)
        self._last_push_time[ch_num] = time.time()

    def _handle_main_param(self, address: str, *args) -> None:
        if not args:
            return
        if "mix/fader" in address:
            with self._state_lock:
                self._state.setdefault(0, {})["main_fader"] = float(args[0])

    def _handle_meters(self, address: str, *args) -> None:
        if not args:
            return
        blob = args[0]
        if not isinstance(blob, (bytes, bytearray)) or len(blob) < 12:
            return
        try:
            parsed = parse_meters_1(bytes(blob))
            with self._state_lock:
                self._meter_rms = parsed["channel_rms"]
                import time as _time
                now_ms = _time.time() * 1000.0
                self._meter_gate_gr  = parsed["gate_gr"]
                self._meter_dyn_gr   = parsed["dynamics_gr"]
                for ch_num in self._channel_map:
                    idx = ch_num - 1
                    if idx < 0 or idx >= 32:
                        continue
                    rms_lin  = self._meter_rms[idx]
                    gate_lin = self._meter_gate_gr[idx]
                    dyn_lin  = self._meter_dyn_gr[idx]
                    rms_db   = linear_to_dbfs(rms_lin)
                    gate_db  = min(0.0, linear_to_dbfs(gate_lin)) if gate_lin < 1.0 else 0.0
                    dyn_db   = min(0.0, linear_to_dbfs(dyn_lin))  if dyn_lin < 1.0 else 0.0
                    prev_state = 'normal'
                    prev_rms   = -90.0
                    if ch_num in self._channel_meters:
                        prev = self._channel_meters[ch_num]
                        prev_rms   = prev.input_rms_db
                        prev_state = prev.input_state
                    cfg = self._channel_configs.get(ch_num)
                    fader_db = cfg.fader_db if cfg else fader_float_to_db(
                        self._state.get(ch_num, {}).get('fader', 0.75))
                    post_fade_db = rms_db + fader_db if rms_db > -88 else -90.0
                    meter = ChannelMeterState(
                        channel_num=ch_num, timestamp_ms=now_ms,
                        input_rms_linear=rms_lin, gate_gr_linear=gate_lin, dyn_gr_linear=dyn_lin,
                        input_rms_db=rms_db, gate_gr_db=gate_db, dyn_gr_db=dyn_db,
                        post_fade_db=post_fade_db, effective_gr_db=gate_db + dyn_db,
                        rms_delta_db=rms_db - prev_rms, input_state=prev_state, prev_input_state=prev_state,
                    )
                    self._channel_meters[ch_num] = meter
        except Exception:
            pass

    def _handle_rta(self, address: str, *args) -> None:
        if not args:
            return
        blob = args[0]
        if not isinstance(blob, (bytes, bytearray)) or len(blob) < 12:
            return
        try:
            rta = parse_meters_15(bytes(blob))
            with self._state_lock:
                self._board_rta_db = rta
        except Exception:
            pass

    def _handle_default(self, address: str, *args) -> None:
        if address == "/info" and args:
            with self._state_lock:
                self._state["_info"] = " ".join(str(a) for a in args)
