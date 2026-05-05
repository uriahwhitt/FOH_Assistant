"""Lightweight X32 OSC simulator for development and testing.

Responds to OSC reads identically to a real X32. Executes scenario timelines
to inject board state changes on a schedule.

Usage:
  python simulator/x32_sim.py --scenario scenarios/level_creep.yaml
  python simulator/x32_sim.py  (no scenario — static baseline state)
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


def db_to_fader_float(d: float) -> float:
    if d < -60.0:
        f = (d + 90.0) / 480.0
    elif d < -30.0:
        f = (d + 70.0) / 160.0
    elif d < -10.0:
        f = (d + 50.0) / 80.0
    else:
        f = (d + 30.0) / 40.0
    return max(0.0, min(1.0, int(f * 1023.5) / 1023.0))


def hz_to_eq_float(hz: float) -> float:
    hz = max(20.0, min(20000.0, hz))
    return (math.log10(hz) - math.log10(20.0)) / (math.log10(20000.0) - math.log10(20.0))


class SimBoard:
    """Internal board state for the simulator."""

    def __init__(self):
        self._lock = threading.Lock()
        # Per-channel state: {ch_num: {param: value}}
        self._channels: dict[int, dict] = {}
        self._main_fader: float = db_to_fader_float(0.0)
        self._init_defaults()

    def _init_defaults(self):
        for ch in range(1, CHANNEL_COUNT + 1):
            self._channels[ch] = {
                "fader": db_to_fader_float(-3.0),
                "on": 1,        # 1 = unmuted
                "pan": 0.0,
                "eq_on": 1,
                **{f"eq_{b}_type": 2 for b in range(1, 5)},
                **{f"eq_{b}_f": hz_to_eq_float(1000.0) for b in range(1, 5)},
                **{f"eq_{b}_g": 0.0 for b in range(1, 5)},
                **{f"eq_{b}_q": 0.707 for b in range(1, 5)},
                "comp_on": 0,
                "comp_thr": -20.0,
                "comp_ratio": 3,
                "gate_on": 0,
                "gate_thr": -40.0,
            }

    def apply_initial_state(self, init: dict) -> None:
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

        if "main_fader_db" in init:
            self._main_fader = db_to_fader_float(init["main_fader_db"])

    def get(self, ch_num: int, param: str):
        with self._lock:
            return self._channels.get(ch_num, {}).get(param)

    def set(self, ch_num: int, param: str, value) -> None:
        with self._lock:
            if ch_num in self._channels:
                self._channels[ch_num][param] = value
                print(f"  SIM: ch{ch_num:02d}/{param} = {value:.4f if isinstance(value, float) else value}")

    def get_main_fader(self) -> float:
        with self._lock:
            return self._main_fader

    def get_meter_blob(self) -> bytes:
        """Return a fake /meters/1 blob with per-channel RMS levels."""
        with self._lock:
            rms_values = []
            for ch in range(1, 33):
                fader = self._channels.get(ch, {}).get("fader", 0.5)
                muted = self._channels.get(ch, {}).get("on", 1) == 0
                if muted:
                    rms_values.append(0.0)
                else:
                    # Approximate: fader at unity (0dB) → 0.3 linear, scale from there
                    rms_values.append(fader * 0.4)
            # Gate GR and dynamics GR — all zeros for simulator
            all_floats = rms_values + [0.0] * 32 + [0.0] * 32

        num_floats = len(all_floats)
        # blob: 4B big-endian size + 4B little-endian count + little-endian floats
        size_bytes = 4 + num_floats * 4
        blob = struct.pack(">I", size_bytes)
        blob += struct.pack("<I", num_floats)
        blob += struct.pack(f"<{num_floats}f", *all_floats)
        return blob


class X32Simulator:
    def __init__(self, port: int = SIM_PORT):
        self._port = port
        self._board = SimBoard()
        self._clients: set = set()
        self._server = None
        self._running = False

    def load_scenario(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        print(f"Scenario: {scenario.get('name', path.stem)}")
        print(f"  {scenario.get('description', '')}")
        if "initial_state" in scenario:
            self._board.apply_initial_state(scenario["initial_state"])
        return scenario

    def start(self, scenario: dict | None = None) -> None:
        disp = dispatcher.Dispatcher()
        disp.map("/xremote", self._handle_xremote)
        disp.map("/xinfo", self._handle_xinfo)
        disp.map("/info", self._handle_info)
        disp.map("/status", self._handle_status)
        disp.map("/node", self._handle_node)
        disp.map("/batchsubscribe", self._handle_batchsubscribe)
        disp.map("/renew", self._handle_renew)
        disp.map("/unsubscribe", self._handle_unsubscribe)
        disp.set_default_handler(self._handle_default)

        self._server = osc_server.ThreadingOSCUDPServer(("0.0.0.0", self._port), disp)
        self._running = True
        print(f"\nX32 Simulator listening on port {self._port}")
        print("Connect FOH Assistant with: python main.py --show --x32-ip 127.0.0.1")
        print("Ctrl+C to stop\n")

        if scenario and "timeline" in scenario:
            timeline_thread = threading.Thread(
                target=self._run_timeline, args=(scenario["timeline"],), daemon=True
            )
            timeline_thread.start()

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
        note = event.get("note", "")
        ch = event.get("channel")

        if action == "fader_move":
            db = event["target_db"]
            self._board.set(ch, "fader", db_to_fader_float(db))
            print(f"\n[{time.strftime('%H:%M:%S')}] SCENARIO: {note or 'fader move'}")

        elif action == "fader_drift":
            # Gradual drift over 'over_s' seconds
            target_db = event["target_db"]
            over_s = event.get("over_s", 10)
            start_fader = self._board.get(ch, "fader")
            if start_fader is None:
                return
            steps = max(1, int(over_s / 0.5))
            target_f = db_to_fader_float(target_db)
            delta = (target_f - start_fader) / steps
            print(f"\n[{time.strftime('%H:%M:%S')}] SCENARIO: {note or 'gradual drift'}")

            def drift():
                current = start_fader
                for _ in range(steps):
                    current += delta
                    self._board.set(ch, "fader", max(0.0, min(1.0, current)))
                    time.sleep(0.5)
            threading.Thread(target=drift, daemon=True).start()

        elif action == "eq_change":
            band = event["band"]
            gain = event["gain_db"]
            self._board.set(ch, f"eq_{band}_g", gain)
            print(f"\n[{time.strftime('%H:%M:%S')}] SCENARIO: {note or 'EQ change'}")

        elif action == "mute":
            muted = event.get("muted", True)
            self._board.set(ch, "on", 0 if muted else 1)
            print(f"\n[{time.strftime('%H:%M:%S')}] SCENARIO: {note or 'mute change'}")

    # ------------------------------------------------------------------
    # OSC handlers
    # ------------------------------------------------------------------

    def _reply(self, client_address, address: str, *args) -> None:
        try:
            client = udp_client.SimpleUDPClient(client_address[0], client_address[1])
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

    def _handle_xremote(self, client_address, address, *args):
        self._clients.add(client_address)

    def _handle_xinfo(self, client_address, address, *args):
        self._reply(client_address, "/xinfo", "127.0.0.1", "X32-SIM", "X32", "3.04-sim")

    def _handle_info(self, client_address, address, *args):
        self._reply(client_address, "/info", "V3.04-sim", "X32-SIM", "X32", "3.04")

    def _handle_status(self, client_address, address, *args):
        self._reply(client_address, "/status", "active", "127.0.0.1", "X32-SIM")

    def _handle_node(self, client_address, address, *args):
        if not args:
            return
        node_path = str(args[0]).strip("/")
        parts = node_path.split("/")

        if len(parts) >= 2 and parts[0] == "ch":
            try:
                ch_num = int(parts[1])
            except ValueError:
                return
            section = parts[2] if len(parts) > 2 else ""

            if section == "mix":
                fader = self._board.get(ch_num, "fader") or 0.75
                on = self._board.get(ch_num, "on")
                on = 1 if on is None else on
                self._reply(client_address, "/node", f"ch/{ch_num:02d}/mix",
                             float(fader), int(on), 0.0)

            elif section == "eq":
                values = []
                for b in range(1, 5):
                    values += [
                        int(self._board.get(ch_num, f"eq_{b}_type") or 2),
                        float(self._board.get(ch_num, f"eq_{b}_f") or 0.5),
                        float(self._board.get(ch_num, f"eq_{b}_g") or 0.0),
                        float(self._board.get(ch_num, f"eq_{b}_q") or 0.707),
                    ]
                self._reply(client_address, "/node", f"ch/{ch_num:02d}/eq", *values)

            elif section == "dyn":
                self._reply(client_address, "/node", f"ch/{ch_num:02d}/dyn",
                             int(self._board.get(ch_num, "comp_on") or 0),
                             float(self._board.get(ch_num, "comp_thr") or -20.0),
                             int(self._board.get(ch_num, "comp_ratio") or 3))

            elif section == "gate":
                self._reply(client_address, "/node", f"ch/{ch_num:02d}/gate",
                             int(self._board.get(ch_num, "gate_on") or 0),
                             float(self._board.get(ch_num, "gate_thr") or -40.0))

        elif len(parts) >= 2 and parts[0] == "main":
            self._reply(client_address, "/node", "main/st/mix",
                         float(self._board.get_main_fader()), 1, 0.0)

    def _handle_batchsubscribe(self, client_address, address, *args):
        # Start pushing meter blobs to this client every ~50ms
        alias = args[0] if args else "/meters"
        def push_meters():
            while self._running and client_address in self._clients:
                blob = self._board.get_meter_blob()
                self._reply(client_address, alias, blob)
                time.sleep(0.05)
        threading.Thread(target=push_meters, daemon=True).start()

    def _handle_renew(self, client_address, address, *args):
        self._clients.add(client_address)

    def _handle_unsubscribe(self, client_address, address, *args):
        self._clients.discard(client_address)

    def _handle_default(self, client_address, address, *args):
        # Handle individual parameter reads: /ch/01/mix/fader etc.
        parts = address.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "ch":
            try:
                ch_num = int(parts[1])
            except ValueError:
                return
            param_path = "/".join(parts[2:])
            val = None
            if param_path == "mix/fader":
                val = float(self._board.get(ch_num, "fader") or 0.75)
                self._reply(client_address, address, val)
            elif param_path == "mix/on":
                val = int(self._board.get(ch_num, "on") or 1)
                self._reply(client_address, address, val)


def main():
    parser = argparse.ArgumentParser(description="X32 Simulator")
    parser.add_argument("--scenario", help="Path to scenario YAML file")
    parser.add_argument("--port", type=int, default=SIM_PORT)
    args = parser.parse_args()

    sim = X32Simulator(port=args.port)
    scenario = None
    if args.scenario:
        scenario = sim.load_scenario(Path(args.scenario))
    sim.start(scenario)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
