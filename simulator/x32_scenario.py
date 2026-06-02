"""X32 Scenario Loader — configures the X32 emulator with a scenario YAML.

Sends OSC write commands to the running X32 emulator to set up a scenario
starting state, then executes timeline events on schedule.

All simulation work goes through the real emulator. main.py reads from it
read-only as always — this tool is the only thing that writes.

Usage:
  python simulator/x32_scenario.py
  python simulator/x32_scenario.py --scenario simulator/scenarios/baseline.yaml
  python simulator/x32_scenario.py --scenario simulator/scenarios/level_creep.yaml
  python simulator/x32_scenario.py --x32-ip 192.168.56.1 --scenario simulator/scenarios/baseline.yaml

Then in another terminal:
  python main.py --show --venue outdoor_patio_june13 --log-level full --x32-ip 192.168.56.1
"""

import argparse
import math
import sys
import time
import threading
from pathlib import Path

import yaml
from pythonosc.udp_client import SimpleUDPClient

DEFAULT_IP   = "192.168.56.1"
DEFAULT_PORT = 10023


# ---------------------------------------------------------------------------
# Conversion utilities (same formulas as x32_sim.py and osc_client.py)
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


def hz_to_eq_float(hz: float) -> float:
    """Convert Hz [20, 20000] to X32 EQ freq float [0.0, 1.0] (log scale)."""
    hz = max(20.0, min(20000.0, hz))
    return (math.log10(hz) - math.log10(20.0)) / (math.log10(20000.0) - math.log10(20.0))


def hpf_slope_to_int(db_oct: int) -> int:
    """Convert dB/octave slope (12/18/24) to X32 enum (0/1/2)."""
    return {12: 0, 18: 1, 24: 2}.get(db_oct, 0)


# ---------------------------------------------------------------------------
# X32 Writer
# ---------------------------------------------------------------------------

class X32Writer:
    """OSC writer for the X32 emulator. One-way — sends only, never reads."""

    def __init__(self, ip: str = DEFAULT_IP, port: int = DEFAULT_PORT):
        self._client = SimpleUDPClient(ip, port)
        self._ip   = ip
        self._port = port

    def send(self, address: str, *args) -> None:
        self._client.send_message(address, list(args))

    # ------------------------------------------------------------------
    # Channel helpers
    # ------------------------------------------------------------------

    def set_fader(self, ch: int, fader_db: float) -> None:
        self.send(f"/ch/{ch:02d}/mix/fader", db_to_fader_float(fader_db))

    def set_mute(self, ch: int, muted: bool) -> None:
        self.send(f"/ch/{ch:02d}/mix/on", 0 if muted else 1)

    def set_eq_band(self, ch: int, band: int,
                    gain_db: float = None, freq_hz: float = None,
                    q: float = None, type_int: int = None) -> None:
        """Set individual EQ band parameters. Pass only the values you want to change."""
        if gain_db is not None:
            self.send(f"/ch/{ch:02d}/eq/{band}/g", float(gain_db))
        if freq_hz is not None:
            self.send(f"/ch/{ch:02d}/eq/{band}/f", hz_to_eq_float(freq_hz))
        if q is not None:
            self.send(f"/ch/{ch:02d}/eq/{band}/q", float(q))
        if type_int is not None:
            self.send(f"/ch/{ch:02d}/eq/{band}/t", int(type_int))

    def set_eq_on(self, ch: int, enabled: bool) -> None:
        self.send(f"/ch/{ch:02d}/eq/on", 1 if enabled else 0)

    def set_hpf(self, ch: int, freq_hz: float, slope_db_oct: int = 12) -> None:
        """Enable HPF at specified frequency. Set freq_hz to 20 or 0 to disable."""
        self.send(f"/ch/{ch:02d}/preamp/hpf",     hz_to_eq_float(max(freq_hz, 20.0)))
        self.send(f"/ch/{ch:02d}/preamp/hpslope", hpf_slope_to_int(slope_db_oct))

    def set_main_fader(self, fader_db: float) -> None:
        self.send("/main/st/mix/fader", db_to_fader_float(fader_db))

    def set_main_mute(self, muted: bool) -> None:
        self.send("/main/st/mix/on", 0 if muted else 1)

    def set_comp(self, ch: int, enabled: bool, threshold_db: float = -20.0,
                 ratio_idx: int = 3, attack_ms: float = 10.0,
                 release_ms: float = 100.0, makeup_db: float = 0.0) -> None:
        self.send(f"/ch/{ch:02d}/dyn/on",      1 if enabled else 0)
        self.send(f"/ch/{ch:02d}/dyn/thr",     float(threshold_db))
        self.send(f"/ch/{ch:02d}/dyn/ratio",   int(ratio_idx))
        self.send(f"/ch/{ch:02d}/dyn/attack",  float(attack_ms))
        self.send(f"/ch/{ch:02d}/dyn/release", float(release_ms))
        self.send(f"/ch/{ch:02d}/dyn/mgain",   float(makeup_db))

    def set_gate(self, ch: int, enabled: bool, threshold_db: float = -40.0,
                 range_db: float = 20.0) -> None:
        self.send(f"/ch/{ch:02d}/gate/on",  1 if enabled else 0)
        self.send(f"/ch/{ch:02d}/gate/thr", float(threshold_db))

    # ------------------------------------------------------------------
    # Bulk channel setup from scenario dict
    # ------------------------------------------------------------------

    def apply_channel_state(self, ch: int, state: dict) -> None:
        """Apply all fields from a scenario channel dict to the emulator."""
        if 'fader_db' in state:
            self.set_fader(ch, state['fader_db'])

        if 'muted' in state:
            self.set_mute(ch, bool(state['muted']))

        if 'eq_on' in state:
            self.set_eq_on(ch, bool(state['eq_on']))

        for band_cfg in state.get('eq', []):
            band = band_cfg.get('band', 1)
            self.set_eq_band(
                ch, band,
                gain_db  = band_cfg.get('gain'),
                freq_hz  = band_cfg.get('freq'),
                q        = band_cfg.get('q'),
                type_int = band_cfg.get('type'),
            )

        if 'hpf_hz' in state:
            slope = state.get('hpf_slope', 12)
            self.set_hpf(ch, state['hpf_hz'], slope)

        comp = state.get('comp', {})
        if comp:
            self.set_comp(
                ch,
                enabled      = bool(comp.get('on', False)),
                threshold_db = comp.get('threshold_db', -20.0),
                ratio_idx    = comp.get('ratio_idx', 3),
                attack_ms    = comp.get('attack_ms', 10.0),
                release_ms   = comp.get('release_ms', 100.0),
                makeup_db    = comp.get('makeup_db', 0.0),
            )

        gate = state.get('gate', {})
        if gate:
            self.set_gate(
                ch,
                enabled      = bool(gate.get('on', False)),
                threshold_db = gate.get('threshold_db', -40.0),
                range_db     = gate.get('range_db', 20.0),
            )


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------

class ScenarioRunner:

    def __init__(self, scenario: dict, writer: X32Writer):
        self._scenario = scenario
        self._writer   = writer
        self._running  = False

    def apply_initial_state(self) -> None:
        """Send all initial_state settings to the emulator."""
        initial = self._scenario.get('initial_state', {})

        channels = initial.get('channels', {})
        for ch_key, ch_state in channels.items():
            ch_num = int(ch_key)
            self._writer.apply_channel_state(ch_num, ch_state)
            time.sleep(0.01)   # small gap between channels to avoid UDP drops

        if 'main_fader_db' in initial:
            self._writer.set_main_fader(initial['main_fader_db'])

        if 'main_muted' in initial:
            self._writer.set_main_mute(bool(initial['main_muted']))

    def run_timeline(self) -> None:
        """Execute timeline events on schedule. Blocks until complete."""
        timeline = self._scenario.get('timeline', [])
        if not timeline:
            return

        self._running = True
        start = time.time()

        for event in sorted(timeline, key=lambda e: e.get('at_s', 0)):
            if not self._running:
                break
            target_t = start + event.get('at_s', 0)
            wait = target_t - time.time()
            if wait > 0:
                time.sleep(wait)
            self._execute_event(event)

    def _execute_event(self, event: dict) -> None:
        action = event.get('action', '')
        ch     = event.get('channel')
        note   = event.get('note', '')
        ts     = time.strftime("%H:%M:%S")

        if action == 'fader_move' and ch is not None:
            target_db = event['target_db']
            drift_s   = event.get('drift_s', 0.0)

            if drift_s > 0 and 'from_db' in event:
                self._drift_fader(ch, event['from_db'], target_db, drift_s)
            else:
                self._writer.set_fader(ch, target_db)

            print(f"[{ts}] ch{ch:02d} fader -> {target_db:+.1f}dB  {note}")

        elif action == 'eq_change' and ch is not None:
            band    = event['band']
            gain_db = event['gain_db']
            self._writer.set_eq_band(ch, band, gain_db=gain_db)
            print(f"[{ts}] ch{ch:02d} EQ band{band} -> {gain_db:+.1f}dB  {note}")

        elif action == 'hpf_change' and ch is not None:
            freq_hz = event['freq_hz']
            slope   = event.get('slope', 12)
            self._writer.set_hpf(ch, freq_hz, slope)
            print(f"[{ts}] ch{ch:02d} HPF -> {freq_hz:.0f}Hz/{slope}dB/oct  {note}")

        elif action == 'mute' and ch is not None:
            muted = bool(event.get('muted', True))
            self._writer.set_mute(ch, muted)
            print(f"[{ts}] ch{ch:02d} {'MUTED' if muted else 'UNMUTED'}  {note}")

        elif action == 'main_fader':
            self._writer.set_main_fader(event['target_db'])
            print(f"[{ts}] Main fader -> {event['target_db']:+.1f}dB  {note}")

        elif action == 'print':
            print(f"[{ts}] --- {note} ---")

    def _drift_fader(self, ch: int, from_db: float, to_db: float,
                      drift_s: float, steps: int = 20) -> None:
        """Gradually move fader from from_db to to_db over drift_s seconds."""
        step_s = drift_s / steps
        for i in range(steps + 1):
            db = from_db + (to_db - from_db) * (i / steps)
            self._writer.set_fader(ch, db)
            time.sleep(step_s)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_scenario(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def list_scenarios() -> list:
    """Return paths to all scenario YAMLs in the standard location."""
    scenarios_dir = Path(__file__).parent / 'scenarios'
    return sorted(scenarios_dir.glob('*.yaml'))


def main() -> None:
    parser = argparse.ArgumentParser(
        description='X32 Scenario Loader — configures the X32 emulator for testing',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python simulator/x32_scenario.py
  python simulator/x32_scenario.py --scenario simulator/scenarios/baseline.yaml
  python simulator/x32_scenario.py --scenario simulator/scenarios/level_creep.yaml --x32-ip 192.168.56.1
  python simulator/x32_scenario.py --list
        """
    )
    parser.add_argument('--scenario', default=None,
                        help='Scenario YAML file to load (default: baseline)')
    parser.add_argument('--x32-ip',  default=DEFAULT_IP,
                        help=f'X32 emulator IP (default: {DEFAULT_IP})')
    parser.add_argument('--port',    type=int, default=DEFAULT_PORT,
                        help=f'X32 OSC port (default: {DEFAULT_PORT})')
    parser.add_argument('--list',    action='store_true',
                        help='List available scenarios and exit')
    parser.add_argument('--no-timeline', action='store_true',
                        help='Apply initial state only, do not run timeline')
    args = parser.parse_args()

    if args.list:
        print("Available scenarios:")
        for p in list_scenarios():
            try:
                with open(p) as f:
                    d = yaml.safe_load(f)
                name = d.get('name', p.stem)
                desc = d.get('description', '').strip().split('\n')[0][:60]
                print(f"  {p.name:<30}  {name}")
                if desc:
                    print(f"  {'':30}  {desc}")
            except Exception:
                print(f"  {p.name}")
        return

    # Resolve scenario path
    if args.scenario:
        scenario_path = Path(args.scenario)
    else:
        scenario_path = Path(__file__).parent / 'scenarios' / 'baseline.yaml'

    if not scenario_path.exists():
        print(f"ERROR: Scenario not found: {scenario_path}")
        sys.exit(1)

    scenario = load_scenario(str(scenario_path))
    name     = scenario.get('name', scenario_path.stem)
    desc     = scenario.get('description', '').strip().split('\n')[0]
    timeline = scenario.get('timeline', [])
    duration = scenario.get('duration_s', 0)

    print(f"\nX32 Scenario Loader")
    print(f"  Emulator:  {args.x32_ip}:{args.port}")
    print(f"  Scenario:  {name}")
    if desc:
        print(f"  Desc:      {desc}")
    print(f"  Channels:  {len(scenario.get('initial_state', {}).get('channels', {}))} configured")
    print(f"  Timeline:  {len(timeline)} events" + (' (skipped)' if args.no_timeline else ''))
    print()

    writer = X32Writer(args.x32_ip, args.port)
    runner = ScenarioRunner(scenario, writer)

    # Apply initial state
    print("Applying initial board state to emulator...")
    try:
        runner.apply_initial_state()
    except Exception as e:
        print(f"ERROR applying state: {e}")
        sys.exit(1)

    print("Board state applied.")
    print()

    # Summary of what was set
    channels = scenario.get('initial_state', {}).get('channels', {})
    print(f"  {'CH':>3}  {'Fader':>7}  {'Muted':>6}  {'HPF':>8}  EQ bands")
    print(f"  {'-'*55}")
    for ch_key in sorted(channels, key=int):
        ch_state = channels[ch_key]
        fader  = ch_state.get('fader_db', 0.0)
        muted  = ch_state.get('muted', False)
        hpf_hz = ch_state.get('hpf_hz')
        eq_bands = ch_state.get('eq', [])
        hpf_str  = f"{hpf_hz:.0f}Hz" if hpf_hz else "default"
        eq_str   = ', '.join(f"b{b.get('band','?')}: {b.get('gain',0):+.1f}dB"
                             for b in eq_bands if b.get('gain', 0) != 0) or "flat"
        mut_str  = "MUTED" if muted else ""
        print(f"  {int(ch_key):>3}  {fader:>+6.1f}dB  {mut_str:>6}  {hpf_str:>8}  {eq_str}")

    main_fader = scenario.get('initial_state', {}).get('main_fader_db')
    if main_fader is not None:
        print(f"\n  Main LR: {main_fader:+.1f}dB")

    if args.no_timeline or not timeline:
        print(f"\nBoard ready. Connect main.py:")
        print(f"  python main.py --show --venue <venue_id> --log-level full --x32-ip {args.x32_ip}")
        print(f"\nCtrl+C to exit (board state stays in emulator)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nExiting — board state preserved in emulator.")
        return

    # Run timeline
    print(f"\nTimeline: {len(timeline)} events over {duration}s")
    print(f"Connect main.py now in another terminal:")
    print(f"  python main.py --show --venue <venue_id> --log-level full --x32-ip {args.x32_ip}")
    print(f"\nTimeline starts in 5 seconds...")
    time.sleep(5)

    try:
        print("Timeline running (Ctrl+C to stop)")
        runner.run_timeline()
        print(f"\nTimeline complete ({len(timeline)} events executed).")
        print("Board state preserved in emulator. Ctrl+C to exit.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runner.stop()
        print("\nStopped.")


if __name__ == '__main__':
    main()
