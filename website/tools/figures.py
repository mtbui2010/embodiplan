"""Render the documentation figures from recorded data.

    python website/tools/figures.py <data-dir>

Writes PNGs into website/docs/assets/img/. Conventions (keep them, they read well on the site):
- one light surface, recessive grid, thin lines, direct labels instead of legends where possible;
- categorical colours in this fixed order: blue, orange, aqua (checked for colour-vision
  deficiency); a sequential quantity uses the blue ramp, light -> dark;
- never a dual y-axis; bars start at zero (use dots + intervals when the interesting range is
  narrow);
- the title states the finding ("… 0 stalls"), not just the variable names;
- every number comes from <data-dir>; nothing is typed in by hand.
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
OUT = Path(__file__).resolve().parents[1] / "docs" / "assets" / "img"
OUT.mkdir(parents=True, exist_ok=True)

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
RAMP = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281"]

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.titlecolor": INK, "axes.titlesize": 11,
    "axes.titleweight": "bold", "axes.labelsize": 9, "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.labelsize": 8, "ytick.labelsize": 8, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.6, "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
    "font.family": "DejaVu Sans", "lines.linewidth": 2, "legend.frameon": False, "legend.fontsize": 8,
})


def save(fig, name):
    fig.savefig(OUT / name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote", OUT / name)


def wilson(k, n, z=1.96):
    """95 % interval for k successes out of n: use it whenever a success rate is plotted."""
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    r = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - r) / d, (c + r) / d


# --- example: replace with real figures -------------------------------------------------------
if __name__ == "__main__":
    print("TODO: add one block per figure, reading from", DATA)
