#!/usr/bin/env python3
"""
graph_csv.py
Generates a battery level graph from an airpods_health_test.py CSV.
Sessions are split when the wall-clock gap between rows exceeds 10 minutes.
If multiple sessions are found, you'll be prompted to choose one.

USAGE:
  python3 graph_csv.py <csv_file>

REQUIREMENTS:
  pip3 install matplotlib seaborn
"""

import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

SESSION_GAP_MIN = 10


def load_csv(path: Path) -> list:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def parse_pct(value) -> Optional[float]:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def parse_ts(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def split_sessions(rows: list) -> list:
    """Split rows into sessions on gaps > SESSION_GAP_MIN minutes."""
    if not rows:
        return []
    sessions, current = [], [rows[0]]
    for prev, curr in zip(rows, rows[1:]):
        ts_prev = parse_ts(prev.get("timestamp", ""))
        ts_curr = parse_ts(curr.get("timestamp", ""))
        if ts_prev and ts_curr:
            gap = (ts_curr - ts_prev).total_seconds() / 60
            if gap > SESSION_GAP_MIN:
                sessions.append(current)
                current = []
        current.append(curr)
    sessions.append(current)
    return sessions


def select_session(sessions: list) -> list:
    """Prompt user to pick a session if there are multiple. Returns rows."""
    if len(sessions) == 1:
        return sessions[0]

    print(f"\nFound {len(sessions)} sessions:\n")
    for i, s in enumerate(sessions, 1):
        ts0 = parse_ts(s[0].get("timestamp", ""))
        ts1 = parse_ts(s[-1].get("timestamp", ""))
        start_str = ts0.strftime("%Y-%m-%d %H:%M") if ts0 else "?"
        end_str   = ts1.strftime("%H:%M")           if ts1 else "?"
        dur_str   = "?"
        if ts0 and ts1:
            dur_min = int((ts1 - ts0).total_seconds() / 60)
            h, m = divmod(dur_min, 60)
            dur_str = f"{h}h {m}m" if h else f"{m}m"
        l0 = parse_pct(s[0].get("left_pct"));  l1 = parse_pct(s[-1].get("left_pct"))
        r0 = parse_pct(s[0].get("right_pct")); r1 = parse_pct(s[-1].get("right_pct"))
        l_str = f"{l0:.0f}%→{l1:.0f}%" if l0 is not None and l1 is not None else "?"
        r_str = f"{r0:.0f}%→{r1:.0f}%" if r0 is not None and r1 is not None else "?"
        print(f"  [{i}]  {start_str} – {end_str}  ({dur_str}, {len(s)} samples)"
              f"   L: {l_str}   R: {r_str}")

    print()
    while True:
        try:
            choice = int(input(f"Select session [1–{len(sessions)}]: ").strip())
            if 1 <= choice <= len(sessions):
                return sessions[choice - 1]
        except (ValueError, EOFError):
            pass
        print(f"  Enter a number between 1 and {len(sessions)}.")


def plot(csv_path: Path, session: list, output_dir: Path = None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
        import seaborn as sns
    except ImportError:
        print("Missing dependencies. Install with: pip3 install matplotlib seaborn")
        sys.exit(1)

    # ── Metadata ──────────────────────────────────────────────────────────────
    model_name  = session[0].get("model_name", "AirPods")
    serial_case = session[0].get("serial_case", "")
    ts0 = parse_ts(session[0].get("timestamp", ""))
    ts1 = parse_ts(session[-1].get("timestamp", ""))

    # ── Data — elapsed minutes from session start ─────────────────────────────
    elapsed, left, right = [], [], []
    for row in session:
        ts = parse_ts(row.get("timestamp", ""))
        elapsed.append((ts - ts0).total_seconds() / 60 if ts and ts0 else None)
        left.append(parse_pct(row.get("left_pct")))
        right.append(parse_pct(row.get("right_pct")))

    # ── Theme ─────────────────────────────────────────────────────────────────
    sns.set_theme(style="whitegrid", font="DejaVu Sans")
    plt.rcParams.update({
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "grid.color":        "#E5E5E5",
        "grid.linewidth":    0.8,
        "axes.edgecolor":    "#CCCCCC",
        "axes.linewidth":    0.8,
        "xtick.color":       "#666666",
        "ytick.color":       "#666666",
        "axes.labelcolor":   "#444444",
        "text.color":        "#333333",
    })

    LEFT_COLOR  = "#E8584A"
    RIGHT_COLOR = "#4A90D9"

    fig, ax = plt.subplots(figsize=(13, 6))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    # ── Lines ─────────────────────────────────────────────────────────────────
    ax.plot(elapsed, left,  color=LEFT_COLOR,  linewidth=2.5,
            marker="o", markersize=5, label="Left",  zorder=3)
    ax.plot(elapsed, right, color=RIGHT_COLOR, linewidth=2.5,
            marker="o", markersize=5, label="Right", zorder=3)

    # ── End-point labels ──────────────────────────────────────────────────────
    for vals, color in [(left, LEFT_COLOR), (right, RIGHT_COLOR)]:
        valid = [(x, y) for x, y in zip(elapsed, vals)
                 if x is not None and y is not None]
        if valid:
            x_last, y_last = valid[-1]
            ax.annotate(f"{y_last:.0f}%", xy=(x_last, y_last),
                        xytext=(9, 0), textcoords="offset points",
                        color=color, fontsize=10.5, fontweight="bold", va="center")

    # ── Axes ──────────────────────────────────────────────────────────────────
    ax.set_xlabel("Elapsed Time (min)", fontsize=12, labelpad=8)
    ax.set_ylabel("Battery Level (%)",  fontsize=12, labelpad=8)
    ax.set_ylim(0, 105)
    ax.set_xlim(left=0)
    ax.yaxis.set_major_locator(ticker.MultipleLocator(10))
    ax.yaxis.set_minor_locator(ticker.MultipleLocator(5))
    ax.tick_params(axis="both", labelsize=10)

    # ── Legend ────────────────────────────────────────────────────────────────
    ax.legend(loc="upper right", fontsize=11, frameon=True,
              framealpha=0.9, edgecolor="#DDDDDD", fancybox=False)

    # ── Title & subtitle ──────────────────────────────────────────────────────
    ax.set_title(model_name, fontsize=15, fontweight="bold",
                 color="#222222", pad=32, loc="left")
    subtitle_parts = [p for p in [serial_case,
                                   ts0.strftime("%Y-%m-%d  %H:%M") if ts0 else ""] if p]
    if subtitle_parts:
        ax.text(0, 1.06, "  ·  ".join(subtitle_parts),
                transform=ax.transAxes, fontsize=10, color="#888888", va="bottom")

    # ── Runtime ───────────────────────────────────────────────────────────────
    if ts0 and ts1:
        dur_min = int((ts1 - ts0).total_seconds() / 60)
        h, m = divmod(dur_min, 60)
        runtime_str = f"{h}h {m}m" if h else f"{m}m"
        ax.text(0.99, 0.03, f"Runtime: {runtime_str}",
                transform=ax.transAxes, fontsize=9.5,
                color="#999999", ha="right", va="bottom")

    # ── Time-to-threshold stats (lower-left) ─────────────────────────────────
    import math

    def time_to_threshold(pct):
        """
        Find first elapsed minute (rounded) where EITHER earbud reaches <= pct.
        Skip-over rule: if sampling jumps over the threshold (e.g. R: 51->49),
        the current row contains the "higher number" (51%) per the stated rule,
        so we always use the current row's elapsed time.
        """
        valid = [(e, l, r) for e, l, r in zip(elapsed, left, right)
                 if e is not None and l is not None and r is not None]
        for i, (e, l, r) in enumerate(valid):
            if l <= pct or r <= pct:
                return round(e)
        return None

    # Session floor — lowest battery reached by either earbud
    valid_left  = [v for v in left  if v is not None]
    valid_right = [v for v in right if v is not None]
    session_floor = min(valid_left + valid_right) if (valid_left or valid_right) else 100

    t80 = time_to_threshold(80)
    t50 = time_to_threshold(50) if session_floor <= 50 else None
    t35 = time_to_threshold(35) if session_floor <= 35 else None

    lines = [f"Time to 80%:  {t80} min" if t80 is not None else "Time to 80%:  not reached"]
    if session_floor <= 50:
        lines.append(f"Time to 50%:  {t50} min" if t50 is not None else "Time to 50%:  not reached")
    if session_floor <= 35:
        lines.append(f"Time to 35%:  {t35} min" if t35 is not None else "Time to 35%:  not reached")

    stats_text = "\n".join(lines)
    ax.text(0.02, 0.05, stats_text,
            transform=ax.transAxes, fontsize=10,
            color="#555555", ha="left", va="bottom",
            linespacing=1.8,
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                      edgecolor="#DDDDDD", alpha=0.85))

    fig.tight_layout(pad=1.5)

    # ── Save ──────────────────────────────────────────────────────────────────
    out_dir  = output_dir if output_dir else Path.cwd()
    out_path = out_dir / f"graph_{csv_path.stem}.png"
    fig.savefig(out_path, dpi=180, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    print(f"\nSaved: {out_path}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("help", "--help", "-h"):
        print(__doc__)
        sys.exit(0)

    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("csv_file")
    parser.add_argument("--output-dir", dest="output_dir", default=None)
    args = parser.parse_args()

    csv_path = Path(args.csv_file).expanduser().resolve()
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else Path.cwd()

    rows     = load_csv(csv_path)
    sessions = split_sessions(rows)
    session  = select_session(sessions)
    plot(csv_path, session, output_dir)


if __name__ == "__main__":
    main()
