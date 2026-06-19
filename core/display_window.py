"""Live 1/3-octave spectrum display window — Phase 2 diagnostic visualization.

Launched by --display flag. Runs in a daemon thread.
Uses matplotlib FuncAnimation at 100ms poll interval; data updates at ~500ms.
"""

import math
import threading
import time
import numpy as np

try:
    import matplotlib
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.animation import FuncAnimation
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

from core.display_buffer import DisplayBuffer
from core.third_octave import N_THIRD_OCTAVE, THIRD_OCTAVE_CENTERS

# ── Colors ──────────────────────────────────────────────────────────────────
BG_COLOR     = '#0d0d0d'
GRID_COLOR   = '#2a2a2a'
ZERO_COLOR   = '#444444'
COLOR_BOARD  = '#d0d0d0'   # board RTA — white/light gray
COLOR_MIC    = '#e8a020'   # room mic — amber/orange
COLOR_TARGET = '#20c0a0'   # genre target — cyan/teal

_EXCESS_COLORS = {
    1.5: ('#604000', 0.25),
    3.0: ('#803800', 0.35),
    5.0: ('#902020', 0.50),
}
_DEFICIENCY_COLORS = {
    1.5: ('#002860', 0.25),
    3.0: ('#001880', 0.35),
    5.0: ('#100060', 0.50),
}

DISPLAY_EMA_ALPHA_BOARD = 0.25   # board RTA — moderate EMA, data is already stable
DISPLAY_EMA_ALPHA_MIC   = 0.12   # room mic — slower EMA, higher measurement noise

MAX_RESTARTS    = 5
RESTART_DELAY_S = 2.0

# ── X-axis ticks (subset of 31 bands, by index) ─────────────────────────────
_TICK_INDICES = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30]
_TICK_LABELS  = ['20', '31', '50', '80', '125', '200', '315', '500',
                 '1k', '1.6k', '2k', '2.5k', '4k', '6.3k', '8k', '20k']

# ── Readout strip: (band_name, hz_label, x_position) ────────────────────────
_READOUT_BANDS = [
    ('sub',       '50Hz',   2),
    ('bass',      '125Hz',  8),
    ('low_mid',   '315Hz',  12),
    ('mid_low',   '750Hz',  15),
    ('mid_high',  '1.5kHz', 18),
    ('upper_mid', '3kHz',   21),
    ('presence',  '6kHz',   24),
    ('air',       '12kHz',  27),
]


def _compute_band_index_ranges() -> dict:
    """Map analysis band names to (start_idx, end_idx+1) in THIRD_OCTAVE_CENTERS."""
    BAND_FREQ_RANGES = {
        'sub':       (20,    80),
        'bass':      (80,    200),
        'low_mid':   (200,   500),
        'mid_low':   (500,   1000),
        'mid_high':  (1000,  2000),
        'upper_mid': (2000,  4000),
        'presence':  (4000,  8000),
        'air':       (8000,  20000),
    }
    result = {}
    for band, (f_lo, f_hi) in BAND_FREQ_RANGES.items():
        indices = [i for i, fc in enumerate(THIRD_OCTAVE_CENTERS)
                   if f_lo <= fc < f_hi]
        if indices:
            result[band] = (min(indices), max(indices) + 1)
    return result


class SpectrumDisplay:
    """Live three-curve 1/3-octave display. Call start() to launch in a daemon thread."""

    def __init__(self, buffer: DisplayBuffer):
        self._buf               = buffer
        self._thread            = None
        self._running           = True
        self._ema_board         = None
        self._ema_mic           = None
        self._band_index_ranges = _compute_band_index_ranges()

    def start(self) -> None:
        """Launch in a daemon thread. Returns immediately."""
        if not MATPLOTLIB_AVAILABLE:
            print("DISPLAY: matplotlib not available — display disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.name = 'SpectrumDisplay'
        self._thread.start()

    def stop(self) -> None:
        """Signal the display thread to shut down cleanly."""
        self._running = False

    def _run(self) -> None:
        """Display loop with automatic restart on failure."""
        attempts = 0
        while self._running and attempts < MAX_RESTARTS:
            try:
                self._ema_board = None
                self._ema_mic   = None
                self._run_once()
            except Exception as e:
                attempts += 1
                if not self._running:
                    break
                if attempts < MAX_RESTARTS:
                    print(f"DISPLAY: window error ({e}) — restarting in {RESTART_DELAY_S}s "
                          f"(attempt {attempts}/{MAX_RESTARTS})")
                    time.sleep(RESTART_DELAY_S)
                else:
                    print(f"DISPLAY: failed after {MAX_RESTARTS} attempts — display disabled")

    def _run_once(self) -> None:
        """Single display session. Called by _run() — restarts on exception."""
        self._fig, axes = plt.subplots(
            2, 1,
            figsize=(12, 7),
            gridspec_kw={'height_ratios': [5, 1], 'hspace': 0.08},
        )
        self._ax      = axes[0]
        self._ax_read = axes[1]

        self._fig.patch.set_facecolor(BG_COLOR)
        self._setup_spectrum_axes()
        self._init_artists()

        self._anim = FuncAnimation(
            self._fig,
            self._update_frame,
            interval=100,
            blit=True,
            cache_frame_data=False,
        )

        plt.tight_layout(pad=1.2)
        plt.show()

        try:
            plt.close(self._fig)
        except Exception:
            pass

    def _setup_spectrum_axes(self) -> None:
        ax = self._ax
        ax.set_facecolor(BG_COLOR)

        ax.set_xlim(-0.5, N_THIRD_OCTAVE - 0.5)
        ax.set_ylim(-12, 12)

        ax.set_xticks(_TICK_INDICES)
        ax.set_xticklabels(_TICK_LABELS, color='#888888', fontsize=8)
        ax.set_yticks([-9, -6, -3, 0, 3, 6, 9])
        ax.yaxis.set_tick_params(labelcolor='#888888', labelsize=8)
        ax.set_ylabel('relative level (normalized shape)', color='#666666', fontsize=8)

        ax.grid(True, which='both', color=GRID_COLOR, linewidth=0.5, alpha=0.6)
        ax.axhline(0, color=ZERO_COLOR, linewidth=1.0, zorder=2)
        ax.text(0.1, 0.3, '0 = mean', color='#444444',
                fontsize=6, fontfamily='monospace', va='bottom')

        self._title = ax.set_title(
            "FOH Assistant  ·  —  ·  —",
            color='#aaaaaa', fontsize=10, pad=8, fontfamily='monospace',
        )
        self._lufs_text = ax.text(
            0.99, 0.97, "LUFS: —",
            transform=ax.transAxes, ha='right', va='top',
            color='#666666', fontsize=8, fontfamily='monospace',
        )
        for spine in ax.spines.values():
            spine.set_edgecolor(GRID_COLOR)

    def _init_artists(self) -> None:
        ax   = self._ax
        x    = np.arange(N_THIRD_OCTAVE)
        zero = np.zeros(N_THIRD_OCTAVE)

        # Band highlight patches (one per analysis band)
        self._highlight_patches = {}
        for band, (idx_lo, idx_hi) in self._band_index_ranges.items():
            patch = mpatches.Rectangle(
                (idx_lo - 0.5, -20), idx_hi - idx_lo, 40,
                facecolor='none', edgecolor='none', alpha=0.0, zorder=1,
            )
            ax.add_patch(patch)
            self._highlight_patches[band] = patch

        # Three step-chart curves
        self._line_board, = ax.step(x, zero, where='mid',
                                     color=COLOR_BOARD, linewidth=1.2,
                                     alpha=0.7, zorder=5, label='Board RTA')
        self._line_mic,   = ax.step(x, zero, where='mid',
                                     color=COLOR_MIC, linewidth=1.8,
                                     alpha=0.9, zorder=6, label='Room Mic')
        self._line_target, = ax.step(x, zero, where='mid',
                                      color=COLOR_TARGET, linewidth=1.2,
                                      alpha=0.6, linestyle='--', zorder=7,
                                      label='Genre Target')

        ax.legend(loc='upper left', fontsize=7,
                  facecolor='#1a1a1a', edgecolor=GRID_COLOR, labelcolor='#aaaaaa')

        # Readout strip
        self._readout_texts = {}
        ax_r = self._ax_read
        ax_r.set_facecolor('#111111')
        ax_r.set_xlim(-0.5, N_THIRD_OCTAVE - 0.5)
        ax_r.set_ylim(-1, 1)
        ax_r.axis('off')

        for band, hz_label, x_pos in _READOUT_BANDS:
            ax_r.text(x_pos, 0.7, hz_label, ha='center', va='center',
                      color='#555555', fontsize=7, fontfamily='monospace')
            txt = ax_r.text(x_pos, -0.2, '—', ha='center', va='center',
                            color='#888888', fontsize=9, fontfamily='monospace',
                            fontweight='bold')
            self._readout_texts[band] = txt

        self._all_artists = (
            list(self._highlight_patches.values()) +
            list(self._readout_texts.values()) +
            [self._line_board, self._line_mic, self._line_target,
             self._title, self._lufs_text]
        )

    def _update_frame(self, frame: int) -> list:
        snap = self._buf.snapshot()

        if snap['is_silent']:
            return self._all_artists

        board_src = snap['board_rta_fast']
        if np.all(board_src == 0):
            board_src = snap['board_rta_bands']

        mic_src = snap['mic_shape_fast']
        if np.all(mic_src == 0):
            mic_src = snap['mic_bands']

        if self._ema_board is None:
            self._ema_board = board_src.copy()
        else:
            self._ema_board = (DISPLAY_EMA_ALPHA_BOARD * board_src +
                               (1 - DISPLAY_EMA_ALPHA_BOARD) * self._ema_board)

        if self._ema_mic is None:
            self._ema_mic = mic_src.copy()
        else:
            self._ema_mic = (DISPLAY_EMA_ALPHA_MIC * mic_src +
                             (1 - DISPLAY_EMA_ALPHA_MIC) * self._ema_mic)

        target = snap['genre_target_bands']

        # Dynamic y-axis: fit all three curves, ignore extreme outlier bands
        all_vals   = np.concatenate([self._ema_board, self._ema_mic, target])
        p_low      = float(np.percentile(all_vals, 5))
        p_high     = float(np.percentile(all_vals, 95))
        data_range = max(p_high - p_low, 6.0)
        center     = (p_high + p_low) / 2.0
        margin     = data_range * 0.6
        y_lo       = max(center - margin, -20.0)
        y_hi       = min(center + margin,  20.0)

        cur_lo, cur_hi = self._ax.get_ylim()
        if abs(y_lo - cur_lo) > 1.0 or abs(y_hi - cur_hi) > 1.0:
            self._ax.set_ylim(y_lo, y_hi)
            tick_step = 3.0 if (y_hi - y_lo) > 12 else 2.0
            ticks = np.arange(math.ceil(y_lo / tick_step) * tick_step,
                              y_hi + tick_step, tick_step)
            self._ax.set_yticks(ticks)
        else:
            y_lo, y_hi = cur_lo, cur_hi

        board_disp  = np.clip(self._ema_board, y_lo, y_hi)
        mic_disp    = np.clip(self._ema_mic,   y_lo, y_hi)
        target_disp = np.clip(target,          y_lo, y_hi)

        self._line_board.set_ydata(board_disp)
        self._line_mic.set_ydata(mic_disp)
        self._line_target.set_ydata(target_disp)

        highlights = snap['band_highlights']
        scale      = self._highlight_intensity_scale(y_lo, y_hi)

        for band, (idx_lo, idx_hi) in self._band_index_ranges.items():
            dev   = highlights.get(band, 0.0)
            patch = self._highlight_patches[band]
            patch.set_x(idx_lo - 0.5)
            patch.set_width(idx_hi - idx_lo)
            patch.set_height(y_hi - y_lo)
            patch.set_y(y_lo)
            if abs(dev) < 1.5:
                patch.set_alpha(0.0)
            else:
                direction = 'excess' if dev > 0 else 'deficiency'
                color, base_alpha = self._pick_highlight_color(abs(dev), direction)
                patch.set_facecolor(color)
                patch.set_alpha(min(0.85, base_alpha * scale))

        for band, txt in self._readout_texts.items():
            dev = highlights.get(band, 0.0)
            txt.set_text(f"{dev:+.1f}" if abs(dev) >= 0.1 else "±0.0")
            if abs(dev) < 1.5:
                txt.set_color('#555555')
            elif dev > 0:
                txt.set_color('#e07010' if abs(dev) < 3.0 else '#d02020')
            else:
                txt.set_color('#2060c0' if abs(dev) < 3.0 else '#4020c0')

        self._title.set_text(
            f"FOH Assistant  ·  {snap['song_name'] or '—'}  ·  {snap['genre_name'] or '—'}"
        )
        lufs = snap['lufs']
        self._lufs_text.set_text(
            f"LUFS: {lufs:.1f}" if lufs > -59 else "LUFS: silent"
        )

        return self._all_artists

    @staticmethod
    def _pick_highlight_color(abs_dev: float, direction: str) -> tuple:
        table  = _EXCESS_COLORS if direction == 'excess' else _DEFICIENCY_COLORS
        result = ('#000000', 0.0)
        for threshold in sorted(table):
            if abs_dev >= threshold:
                result = table[threshold]
        return result

    @staticmethod
    def _highlight_intensity_scale(y_lo: float, y_hi: float) -> float:
        """Scale highlight opacity relative to 24dB reference range."""
        current_range   = y_hi - y_lo
        reference_range = 24.0
        return min(1.5, reference_range / max(current_range, 6.0))


def launch_display(buffer: DisplayBuffer) -> 'SpectrumDisplay | None':
    """Try to launch the display window. Returns SpectrumDisplay or None if unavailable."""
    if not MATPLOTLIB_AVAILABLE:
        print("DISPLAY: matplotlib not installed — run 'pip install matplotlib'")
        return None

    for backend in ('TkAgg', 'Qt5Agg', 'Agg'):
        try:
            matplotlib.use(backend)
            display = SpectrumDisplay(buffer)
            display.start()
            print(f"DISPLAY: spectrum window launching (backend: {backend})")
            return display
        except Exception:
            continue

    print("DISPLAY: no compatible backend found — display disabled")
    return None
