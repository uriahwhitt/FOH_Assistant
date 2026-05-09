"""Lightweight X32 OSC simulator for development and testing.

Responds to OSC reads identically to a real X32 on port 10023.
Executes scenario timelines to inject board-state changes on a schedule.

Key implementation note:
  All dispatcher.map() calls use needs_reply_address=True so each handler
  receives the client's (ip, port) as its first argument.  Without this flag,
  python-osc passes the OSC address string as the first arg and _reply() would
  crash trying to subscript it as a tuple.

Usage:
  python simulator/x32_sim.py --scenario simulator/scenarios/level_creep.yaml
  python simulator/x32_sim.py          # static baseline state, no timeline
"""

import argparse
import math
import struct
import sys
import time
import threading
from pathlib import Path

import yaml
from pythonosc import dispatcher, osc_server, udp_client
from pythonosc.osc_message_builder import OscMessageBuilder

SIM_PORT = 10023
CHANNEL_COUNT = 32


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

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
    return max(0.0, min(1.0, int(f * 1023.5) / 1023.0))


def fader_float_to_db(f: float) -> float:
    """Convert X32 fader float [0.0, 1.0] to dB [-90, +10]."""
    if f >= 0.5:
        return f * 40.0 - 30.0
    elif f >= 0.25:
        return f * 80.0 - 50.0
    elif f >= 0.0625:
        return f * 160.0 - 70.0
    elif f > 0.0:
        return f * 480.0 - 90.0
    return -90.0


def hz_to_eq_float(hz: float) -> float:
    """Convert Hz [20, 20000] to X32 EQ freq float [0.0, 1.0] (log scale)."""
    hz = max(20.0, min(20000.0, hz))
    return (math.log10(hz) - math.log10(20.0)) / (math.log10(20000.0) - math.log10(20.0))


# ---------------------------------------------------------------------------
# Internal board state
# ---------------------------------------------------------------------------

class SimBoard:
    """Thread-safe internal board state for the simulator."""

    def __init__(self):
        self._lock = threading.Lock()
        self._channels: dict[int, dict] = {}
        self._main_fader: float = db_to_fader_float(0.0)
        self._init_defaults()

    def _init_defaults(self) -> None:
        for ch in range(1, CHANNEL_COUNT + 1):
            self._channels[ch] = {
                "fader":    db_to_fader_float(-3.0),
                "on":       1,          # 1 = unmuted
                "pan":      0.0,
                "eq_on":    1,
                "name":     f"CH{ch:02d}",
                **{f"eq_{b}_type": 2              for b in range(1, 5)},
                **{f"eq_{b}_f":    hz_to_eq_float(1000.0) for b in range(1, 5)},
                **{f"eq_{b}_g":    0.0             for b in range(1, 5)},
                **{f"eq_{b}_q":    0.707           for b in range(1, 5)},
                "comp_on":   0,
                "comp_thr": -20.0,
                "comp_ratio": 3,
                "gate_on":   0,
                "gate_thr": -40.0,
            }

    def apply_initial_state(self, init: dict) -> None:
        """Apply initial_state block from a scenario YAML."""
        channels = init.get("channels", {})
        for ch_num_raw, params in channels.items():
            ch_num = int(ch_num_raw)
            if ch_num not in self._channels:
                continue
            if "fader_db" in params:
                self._channels[ch_num]["fader"] = db_to_fader_float(params["fader_db"])
            if "muted" in params:
                self._channels[ch_num]["on"] = 0 if params["muted"] else 1
            if "eq" in params and isinstance(params["eq"], list):
                for eq_entry in params["eq"]:
                    b = eq_entry.get("band", 1)
                    if "gain" in eq_entry:
                        self._channels[ch_num][f"eq_{b}_g"] = float(eq_entry["gain"])
                    if "freq" in eq_entry:
                        self._channels[ch_num][f"eq_{b}_f"] = hz_to_eq_float(float(eq_entry["freq"]))
                    if "q" in eq_entry:
                        self._channels[ch_num][f"eq_{b}_q"] = float(eq_entry["q"])
                    if "type" in eq_entry:
                        self._channels[ch_num][f"eq_{b}_type"] = int(eq_entry["type"])

        if "main_fader_db" in init:
            self._main_fader = db_to_fader_float(init["main_fader_db"])

    def get(self, ch_num: int, param: str):
        with self._lock:
            return self._channels.get(ch_num, {}).get(param)

    def set(self, ch_num: int, param: str, value, silent: bool = False) -> None:
        """Set a channel parameter.  Pass silent=True to suppress terminal output
        (used for drift increments so the terminal isn't flooded)."""
        with self._lock:
            if ch_num in self._channels:
                self._channels[ch_num][param] = value
                if not silent:
                    fmt = f"{value:.4f}" if isinstance(value, float) else str(value)
                    print(f"  SIM: ch{ch_num:02d}/{param} = {fmt}")

    def get_main_fader(self) -> float:
        with self._lock:
            return self._main_fader

    def get_meter_blob(self) -> bytes:
        """Return a /meters/1-compatible blob with per-channel RMS levels.

        RMS is approximated as fader_float * 0.4 linear.  Muted channels
        return 0.0.  Three groups of 32 floats: channel RMS, gate GR,
        dynamics GR (last two are all zeros for the simulator).
        """
        with self._lock:
            rms_values = []
            for ch in range(1, 33):
                fader = self._channels.get(ch, {}).get("fader", 0.5)
                muted = self._channels.get(ch, {}).get("on", 1) == 0
                rms_values.append(0.0 if muted else fader * 0.4)
            all_floats = rms_values + [0.0] * 32 + [0.0] * 32   # 96 total

        num_floats = len(all_floats)
        size_bytes = 4 + num_floats * 4     # what the blob size field encodes
        blob  = struct.pack(">I", size_bytes)
        blob += struct.pack("<I", num_floats)
        blob += struct.pack(f"<{num_floats}f", *all_floats)
        return blob


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class X32Simulator:
    def __init__(self, port: int = SIM_PORT):
        self._port = port
        self._board = SimBoard()
        self._clients: set = set()
        self._server = None
        self._running = False

    # ------------------------------------------------------------------
    # Scenario loading
    # ------------------------------------------------------------------

    def load_scenario(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        print(f"Scenario: {scenario.get('name', path.stem)}")
        desc = scenario.get("description", "")
        if desc:
            print(f"  {desc[:120].splitlines()[0]}")
        if "initial_state" in scenario:
            self._board.apply_initial_state(scenario["initial_state"])
        return scenario

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def start(self, scenario: dict | None = None) -> None:
        disp = dispatcher.Dispatcher()
        # needs_reply_address=True: each handler receives (client_address, address, *args)
        # Without this flag python-osc passes (address, *args) and client_address gets
        # the OSC address string — _reply() would crash subscripting a string as a tuple.
        disp.map("/xremote",       self._handle_xremote,       needs_reply_address=True)
        disp.map("/xinfo",         self._handle_xinfo,         needs_reply_address=True)
        disp.map("/info",          self._handle_info,          needs_reply_address=True)
        disp.map("/status",        self._handle_status,        needs_reply_address=True)
        disp.map("/node",          self._handle_node,          needs_reply_address=True)
        disp.map("/batchsubscribe",self._handle_batchsubscribe,needs_reply_address=True)
        disp.map("/renew",         self._handle_renew,         needs_reply_address=True)
        disp.map("/unsubscribe",   self._handle_unsubscribe,   needs_reply_address=True)
        disp.set_default_handler(self._handle_default,         needs_reply_address=True)

        self._server = osc_server.ThreadingOSCUDPServer(("0.0.0.0", self._port), disp)
        self._running = True
        print(f"\nX32 Simulator listening on 0.0.0.0:{self._port}")
        print("Connect: python main.py --show --x32-ip 127.0.0.1")
        print("Ctrl+C to stop\n")

        if scenario and "timeline" in scenario:
            threading.Thread(
                target=self._run_timeline, args=(scenario["timeline"],), daemon=True
            ).start()

        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            print("\nSimulator stopped.")

    # ------------------------------------------------------------------
    # Timeline execution
    # ------------------------------------------------------------------

    def _run_timeline(self, timeline: list) -> None:
        start = time.time()
        for event in sorted(timeline, key=lambda e: e.get("at_s", 0)):
            target_t = start + event["at_s"]
            wait = target_t - time.time()
            if wait > 0:
                time.sleep(wait)
            if not self._running:
                break
            self._execute_event(event)

    def _execute_event(self, event: dict) -> None:
        action = event.get("action")
        note   = event.get("note", "")
        ch     = event.get("channel")
        ts     = time.strftime("%H:%M:%S")

        if action == "fader_move":
            db      = event["target_db"]
            fader_f = db_to_fader_float(db)
            self._board.set(ch, "fader", fader_f)
            self._push_to_clients(f"/ch/{ch:02d}/mix/fader", float(fader_f))
            print(f"\n[{ts}] SCENARIO ch{ch:02d}: {note or 'fader move'} → {db:+.1f} dB")

        elif action == "fader_drift":
            target_db = event["target_db"]
            over_s    = event.get("over_s", 10)
            start_f   = self._board.get(ch, "fader")
            if start_f is None:
                return
            start_db = fader_float_to_db(start_f)
            target_f  = db_to_fader_float(target_db)
            steps     = max(1, int(over_s / 0.5))    # one step every 500ms
            delta     = (target_f - start_f) / steps
            print(f"\n[{ts}] SCENARIO ch{ch:02d}: {note or 'fader drift'} "
                  f"{start_db:+.1f} → {target_db:+.1f} dB over {over_s}s")

            def drift(board=self._board, channel=ch, steps=steps,
                      delta=delta, start=start_f, sim=self):
                current = start
                for _ in range(steps):
                    current = max(0.0, min(1.0, current + delta))
                    board.set(channel, "fader", current, silent=True)
                    sim._push_to_clients(f"/ch/{channel:02d}/mix/fader", float(current))
                    time.sleep(0.5)
                final_db = fader_float_to_db(board.get(channel, "fader") or 0.0)
                print(f"  SIM: ch{channel:02d} drift complete → {final_db:+.1f} dB")

            threading.Thread(target=drift, daemon=True).start()

        elif action == "eq_change":
            band = event["band"]
            gain = event["gain_db"]
            self._board.set(ch, f"eq_{band}_g", float(gain))
            self._push_to_clients(f"/ch/{ch:02d}/eq/{band}/g", float(gain))
            print(f"\n[{ts}] SCENARIO ch{ch:02d}: {note or 'EQ change'} "
                  f"band {band} → {gain:+.1f} dB")

        elif action == "mute":
            muted   = event.get("muted", True)
            on_val  = 0 if muted else 1
            self._board.set(ch, "on", on_val)
            self._push_to_clients(f"/ch/{ch:02d}/mix/on", on_val)
            state = "MUTED" if muted else "UNMUTED"
            print(f"\n[{ts}] SCENARIO ch{ch:02d}: {note or 'mute change'} → {state}")

    # ------------------------------------------------------------------
    # Push broadcast — send a parameter update to all registered clients
    # ------------------------------------------------------------------

    def _push_to_clients(self, address: str, *args) -> None:
        """Broadcast an OSC parameter change to every client registered via /xremote.

        This mirrors what a real X32 does automatically: any change to the board
        state is pushed to all registered subscribers.  Without this, clients that
        rely on /xremote push (rather than polling) will never see timeline changes.
        """
        for client_addr in list(self._clients):
            self._reply(client_addr, address, *args)

    # ------------------------------------------------------------------
    # OSC reply helper
    # ------------------------------------------------------------------

    def _reply(self, client_address: tuple, address: str, *args) -> None:
        """Send an OSC reply to client_address using a transient SimpleUDPClient."""
        try:
            client  = udp_client.SimpleUDPClient(client_address[0], client_address[1])
            builder = OscMessageBuilder(address=address)
            for arg in args:
                if isinstance(arg, float):
                    builder.add_arg(arg, "f")
                elif isinstance(arg, int):
                    builder.add_arg(arg, "i")
                elif isinstance(arg, str):
                    builder.add_arg(arg, "s")
                elif isinstance(arg, (bytes, bytearray)):
                    builder.add_arg(bytes(arg), "b")
            client.send(builder.build())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # OSC message handlers (all receive client_address as first arg)
    # ------------------------------------------------------------------

    def _handle_xremote(self, client_address, address, *args):
        self._clients.add(client_address)

    def _handle_xinfo(self, client_address, address, *args):
        self._reply(client_address, "/xinfo",
                    "127.0.0.1", "X32-SIM", "X32", "3.04-sim")

    def _handle_info(self, client_address, address, *args):
        self._reply(client_address, "/info",
                    "V3.04-sim", "X32-SIM", "X32", "3.04")

    def _handle_status(self, client_address, address, *args):
        self._reply(client_address, "/status",
                    "active", "127.0.0.1", "X32-SIM")

    def _handle_node(self, client_address, address, *args):
        if not args:
            return
        node_path = str(args[0]).strip("/")
        parts     = node_path.split("/")

        if len(parts) >= 3 and parts[0] == "ch":
            try:
                ch_num = int(parts[1])
            except ValueError:
                return
            section = parts[2]

            if section == "mix":
                fader = self._board.get(ch_num, "fader") or 0.75
                on    = self._board.get(ch_num, "on")
                on    = 1 if on is None else on
                self._reply(client_address, "/node",
                             f"ch/{ch_num:02d}/mix",
                             float(fader), int(on), 0.0)

            elif section == "eq":
                values = []
                for b in range(1, 5):
                    values += [
                        int(self._board.get(ch_num, f"eq_{b}_type") or 2),
                        float(self._board.get(ch_num, f"eq_{b}_f")    or 0.5),
                        float(self._board.get(ch_num, f"eq_{b}_g")    or 0.0),
                        float(self._board.get(ch_num, f"eq_{b}_q")    or 0.707),
                    ]
                self._reply(client_address, "/node",
                             f"ch/{ch_num:02d}/eq", *values)

            elif section == "dyn":
                self._reply(client_address, "/node",
                             f"ch/{ch_num:02d}/dyn",
                             int(self._board.get(ch_num, "comp_on")    or 0),
                             float(self._board.get(ch_num, "comp_thr") or -20.0),
                             int(self._board.get(ch_num, "comp_ratio") or 3))

            elif section == "gate":
                self._reply(client_address, "/node",
                             f"ch/{ch_num:02d}/gate",
                             int(self._board.get(ch_num, "gate_on")    or 0),
                             float(self._board.get(ch_num, "gate_thr") or -40.0))

            elif section == "config":
                name = self._board.get(ch_num, "name") or f"CH{ch_num:02d}"
                self._reply(client_address, "/node",
                             f"ch/{ch_num:02d}/config",
                             f'"{name}" 0 0 0')

        elif len(parts) >= 2 and parts[0] == "main":
            self._reply(client_address, "/node",
                         "main/st/mix",
                         float(self._board.get_main_fader()), 1, 0.0)

    def _handle_batchsubscribe(self, client_address, address, *args):
        """Start pushing meter blobs to this client every 50ms."""
        alias = args[0] if args else "/meters"
        self._clients.add(client_address)

        def push_meters(addr=alias, ca=client_address):
            while self._running and ca in self._clients:
                blob = self._board.get_meter_blob()
                self._reply(ca, addr, blob)
                time.sleep(0.05)

        threading.Thread(target=push_meters, daemon=True).start()

    def _handle_renew(self, client_address, address, *args):
        self._clients.add(client_address)

    def _handle_unsubscribe(self, client_address, address, *args):
        self._clients.discard(client_address)

    def _handle_default(self, client_address, address, *args):
        """Handle individual parameter reads: /ch/01/mix/fader, etc."""
        parts = address.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "ch":
            try:
                ch_num = int(parts[1])
            except ValueError:
                return
            param_path = "/".join(parts[2:])
            if param_path == "mix/fader":
                val = float(self._board.get(ch_num, "fader") or 0.75)
                self._reply(client_address, address, val)
            elif param_path == "mix/on":
                val = int(self._board.get(ch_num, "on") or 1)
                self._reply(client_address, address, val)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="X32 Simulator")
    parser.add_argument("--scenario", help="Path to scenario YAML file")
    parser.add_argument("--port",     type=int, default=SIM_PORT,
                        help=f"UDP port to listen on (default {SIM_PORT})")
    args = parser.parse_args()

    sim      = X32Simulator(port=args.port)
    scenario = None
    if args.scenario:
        scenario = sim.load_scenario(Path(args.scenario))
    sim.start(scenario)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
