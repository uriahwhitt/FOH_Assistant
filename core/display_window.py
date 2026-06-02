"""Live spectrum display window — Phase 2 diagnostic visualization tool.

Launched by --display flag. Runs in a daemon thread.
Uses matplotlib FuncAnimation at 10fps.
"""

import threading
import numpy as np

try:
    import matplotlib
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.animation import FuncAnimation
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

from core.channel_model import FREQ_AXIS, N_FREQS
from core.display_buffer import DisplayBuffer

# ── Colors ─────────────────────────────────────────────────────────────────
BG_COLOR    = '#0d0d0d'
GRID_COLOR  = '#2a2a2a'
ZERO_COLOR  = '#444444'
COLOR_BOARD  = '#d0d0d0'   # board RTA — white/light gray
COLOR_MIC    = '#e8a020'   # room mic — amber/orange
COLOR_TARGET = '#20c0a0'   # genre target — cyan/teal

# Band highlight fill colors: threshold_db → (facecolor, alpha)
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

DISPLAY_EMA_ALPHA = 0.15   # display-layer smoothing — cosmetic only

BAND_EDGES = {
    'sub':       (20,    80),
    'bass':      (80,    200),
    'low_mid':   (200,   500),
    'mid_low':   (500,   1000),
    'mid_high':  (1000,  2000),
    'upper_mid': (2000,  4000),
    'presence':  (4000,  8000),
    'air':       (8000,  18660),
}
BAND_LABELS = {
    'sub': 'SUB', 'bass': 'BASS', 'low_mid': 'L-MID', 'mid_low': 'MID-L',
    'mid_high': 'MID-H', 'upper_mid': 'U-MID', 'presence': 'PRES', 'air': 'AIR',
}
READOUT_BANDS = [
    ('sub',       '50Hz'),
    ('bass',      '125Hz'),
    ('low_mid',   '315Hz'),
    ('mid_low',   '750Hz'),
    ('mid_high',  '1.5kHz'),
    ('upper_mid', '3kHz'),
    ('presence',  '6kHz'),
    ('air',       '12kHz'),
]


class SpectrumDisplay:
    """Live three-curve spectrum display. Call start() to launch in a daemon thread."""

    def __init__(self, buffer: DisplayBuffer):
        self._buf       = buffer
        self._thread    = None
        self._ema_board = None
        self._ema_mic   = None

    def start(self) -> None:
        """Launch in a daemon thread. Returns immediately."""
        if not MATPLOTLIB_AVAILABLE:
            print("DISPLAY: matplotlib not available — display disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.name = 'SpectrumDisplay'
        self._thread.start()

    def _run(self) -> None:
        try:
            self._fig, axes = plt.subplots(
                2, 1,
                figsize=(12, 7),
                gridspec_kw={'height_ratios': [5, 1], 'hspace': 0.08},
            )
            self._ax      = axes[0]
            self._ax_read = axes[1]

            self._fig.patch.set_facecolor(BG_COLOR)
            self._setup_spectrum_axes()
            self._setup_readout_axes()
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
        except Exception as e:
            print(f"DISPLAY: window error — {e}")

    def _setup_spectrum_axes(self) -> None:
        ax = self._ax
        ax.set_facecolor(BG_COLOR)
        ax.set_xscale('log')
        ax.set_xlim(20, 18660)
        ax.set_ylim(-14, 14)
        ax.set_yticks([-12, -9, -6, -3, 0, 3, 6, 9, 12])
        ax.yaxis.set_tick_params(labelcolor='#888888', labelsize=8)
        ax.set_ylabel('dB (normalized shape)', color='#666666', fontsize=8)
        ax.set_xticklabels([])

        ax.grid(True, which='major', color=GRID_COLOR, linewidth=0.5, alpha=0.8)
        ax.grid(True, which='minor', color=GRID_COLOR, linewidth=0.3, alpha=0.4)
        ax.axhline(0, color=ZERO_COLOR, linewidth=1.0, zorder=2)

        for band, (f_lo, f_hi) in BAND_EDGES.items():
            center_log = 10 ** ((np.log10(f_lo) + np.log10(f_hi)) / 2)
            ax.text(center_log, -13.4, BAND_LABELS[band],
                    ha='center', va='bottom', color='#555555',
                    fontsize=6.5, fontfamily='monospace')

        for _, (_, f_hi) in list(BAND_EDGES.items())[:-1]:
            ax.axvline(f_hi, color='#333333', linewidth=0.5, alpha=0.6, zorder=1)

        self._title = ax.set_title(
            "FOH Assistant  ·  —  ·  —",
            color='#aaaaaa', fontsize=10, pad=6, fontfamily='monospace',
        )
        self._lufs_text = ax.text(
            0.995, 0.97, "LUFS: —",
            transform=ax.transAxes, ha='right', va='top',
            color='#666666', fontsize=8, fontfamily='monospace',
        )
        for spine in ax.spines.values():
            spine.set_edgecolor(GRID_COLOR)

    def _setup_readout_axes(self) -> None:
        ax = self._ax_read
        ax.set_facecolor('#111111')
        ax.set_xlim(0, len(READOUT_BANDS))
        ax.set_ylim(-1, 1)
        ax.axis('off')

    def _init_artists(self) -> None:
        ax = self._ax

        self._highlight_patches = {}
        for band, (f_lo, f_hi) in BAND_EDGES.items():
            patch = mpatches.Rectangle(
                (f_lo, -14), f_hi - f_lo, 28,
                facecolor='none', edgecolor='none', alpha=0.0, zorder=1,
            )
            ax.add_patch(patch)
            self._highlight_patches[band] = patch

        self._peak_lines = {}
        for band in BAND_EDGES:
            line, = ax.plot([], [], color='#ffffff', linewidth=1.0,
                            alpha=0.0, zorder=4)
            self._peak_lines[band] = line

        self._line_board, = ax.plot(
            FREQ_AXIS, np.zeros(N_FREQS),
            color=COLOR_BOARD, linewidth=1.1, alpha=0.65, zorder=5, label='Board RTA',
        )
        self._line_mic, = ax.plot(
            FREQ_AXIS, np.zeros(N_FREQS),
            color=COLOR_MIC, linewidth=1.8, alpha=0.90, zorder=6, label='Room Mic',
        )
        self._line_target, = ax.plot(
            FREQ_AXIS, np.zeros(N_FREQS),
            color=COLOR_TARGET, linewidth=1.2, alpha=0.60,
            linestyle='--', zorder=7, label='Genre Target',
        )

        ax.legend(
            loc='upper left', fontsize=7,
            facecolor='#1a1a1a', edgecolor=GRID_COLOR, labelcolor='#aaaaaa',
        )

        self._readout_texts = {}
        ax_r = self._ax_read
        for i, (band, hz_label) in enumerate(READOUT_BANDS):
            x = i + 0.5
            ax_r.text(x, 0.7, hz_label, ha='center', va='center',
                      color='#555555', fontsize=7, fontfamily='monospace')
            txt = ax_r.text(x, -0.2, '—', ha='center', va='center',
                            color='#888888', fontsize=9, fontfamily='monospace',
                            fontweight='bold')
            self._readout_texts[band] = txt

        self._all_artists = (
            list(self._highlight_patches.values()) +
            list(self._peak_lines.values()) +
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
            board_src = snap['board_rta_shape']

        mic_src = snap['mic_shape_fast']
        if np.all(mic_src == 0):
            mic_src = snap['mic_shape']

        if self._ema_board is None:
            self._ema_board = board_src.copy()
        else:
            self._ema_board = (DISPLAY_EMA_ALPHA * board_src +
                               (1 - DISPLAY_EMA_ALPHA) * self._ema_board)

        if self._ema_mic is None:
            self._ema_mic = mic_src.copy()
        else:
            self._ema_mic = (DISPLAY_EMA_ALPHA * mic_src +
                             (1 - DISPLAY_EMA_ALPHA) * self._ema_mic)

        self._line_board.set_ydata(np.clip(self._ema_board, -14, 14))
        self._line_mic.set_ydata(np.clip(self._ema_mic,   -14, 14))
        self._line_target.set_ydata(np.clip(snap['genre_target'], -14, 14))

        highlights = snap['band_highlights']
        peaks      = snap['band_peaks']

        for band, (f_lo, f_hi) in BAND_EDGES.items():
            dev   = highlights.get(band, 0.0)
            patch = self._highlight_patches[band]
            pline = self._peak_lines[band]

            if abs(dev) < 1.5:
                patch.set_alpha(0.0)
                pline.set_alpha(0.0)
            else:
                direction     = 'excess' if dev > 0 else 'deficiency'
                color, alpha  = self._pick_highlight_color(abs(dev), direction)
                patch.set_facecolor(color)
                patch.set_alpha(alpha)

                if band in peaks:
                    peak_hz, prominence = peaks[band]
                    if f_lo <= peak_hz <= f_hi and prominence > 0.5:
                        pline.set_data([peak_hz, peak_hz], [-13, 13])
                        pline.set_alpha(0.35)
                    else:
                        pline.set_alpha(0.0)
                else:
                    pline.set_alpha(0.0)

        for band, _ in READOUT_BANDS:
            dev = highlights.get(band, 0.0)
            txt = self._readout_texts[band]
            txt.set_text(f"{dev:+.1f}" if abs(dev) >= 0.1 else "±0.0")
            if abs(dev) < 1.5:
                txt.set_color('#555555')
            elif dev > 0:
                txt.set_color('#e07010' if abs(dev) < 3.0 else '#d02020')
            else:
                txt.set_color('#2060c0' if abs(dev) < 3.0 else '#4020c0')

        song  = snap['song_name'] or '—'
        genre = snap['genre_name'] or '—'
        self._title.set_text(f"FOH Assistant  ·  {song}  ·  {genre}")
        lufs_val = snap['lufs']
        self._lufs_text.set_text(
            f"LUFS: {lufs_val:.1f}" if lufs_val > -59 else "LUFS: silent"
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
