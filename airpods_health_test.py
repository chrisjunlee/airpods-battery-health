#!/usr/bin/env python3
"""
airpods_health_test.py
AirPods Battery Health Test

USAGE:
  python3 airpods_health_test.py
  python3 airpods_health_test.py --serial DD609JF2M7
  python3 airpods_health_test.py --interval 2 --threshold 15
  python3 airpods_health_test.py --output ~/AirPodsTests
  python3 airpods_health_test.py help

REQUIREMENTS:
  - macOS (Ventura / Sonoma / Sequoia / Tahoe)
  - brew install switchaudio-osx   (device detection)
  - brew install sox               (pink noise; falls back to system tone)
  - AirPods connected as audio output before running
"""

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple


# ── Terminal colors ────────────────────────────────────────────────────────────
class C:
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"
    # Cursor control
    UP     = "\033[{}A"   # move cursor up N lines
    CLEAR  = "\033[2K"    # erase current line


def log(msg):       print(f"{C.CYAN}[{_ts()}]{C.RESET} {msg}")
def log_ok(msg):    print(f"{C.GREEN}[{_ts()}] ✓{C.RESET} {msg}")
def log_warn(msg):  print(f"{C.YELLOW}[{_ts()}] ⚠{C.RESET}  {msg}")
def log_err(msg):   print(f"{C.RED}[{_ts()}] ✗{C.RESET}  {msg}", file=sys.stderr)
def log_head(msg):  print(f"\n{C.BOLD}{C.CYAN}── {msg} ──{C.RESET}")
def _ts():          return datetime.now().strftime("%H:%M:%S")


# ── Model lookup ───────────────────────────────────────────────────────────────
# Key: product_id integer (from system_profiler JSON device_productID)
# Value: (model_name, model_number, camel_name)
# Sources: Apple support, ChatGPT cross-reference, github.com/maniacx/Bluetooth-Battery-Meter/issues/65
# Format: (display_name, apple_model_number, camelCaseFilename)
MODEL_DB = {
    0x2002: ("AirPods 1",             "A1523", "airpods1"),
    0x200F: ("AirPods 2",             "A2031", "airpods2"),
    0x2013: ("AirPods 3",             "A2565", "airpods3"),
    0x2019: ("AirPods 4",             "A3048", "airpods4"),
    0x201B: ("AirPods 4 ANC",         "A3049", "airpods4ANC"),
    0x200E: ("AirPods Pro 1",         "A2084", "airpodsPro1"),
    0x2014: ("AirPods Pro 2",         "A2699", "airpodsPro2"),  # Lightning case
    0x2024: ("AirPods Pro 2",         "A3047", "airpodsPro2"),  # USB-C case
    0x2027: ("AirPods Pro 3",         "A3064", "airpodsPro3"), 
    0x200A: ("AirPods Max 1",         "A2096", "airpodsMax1"),  # Lightning
    0x201F: ("AirPods Max 2",         "A3106", "airpodsMax2"),  # USB-C
}


def lookup_model(product_id: int):
    """Return (model_name, model_number, camel_name) or None."""
    return MODEL_DB.get(product_id)


def infer_camel(name: str) -> str:
    """Derive a camelCase identifier from a free-form device name (fallback)."""
    words = re.sub(r"[^a-z0-9 ]", "", name.lower()).split()
    if not words:
        return "airpods"
    return words[0] + "".join(w.capitalize() for w in words[1:])


# ── Device detection ───────────────────────────────────────────────────────────
def run(cmd: list[str], check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def get_current_audio_bt_address() -> str:
    """
    Use SwitchAudioSource to find the current audio output device.
    Returns BT address in colon format e.g. "50:F3:51:CD:B8:D8".
    """
    try:
        result = run(["SwitchAudioSource", "-c", "-f", "json"])
    except FileNotFoundError:
        raise RuntimeError("SwitchAudioSource not found. Install: brew install switchaudio-osx")

    data = json.loads(result.stdout.strip())
    uid = data.get("uid", "")  # e.g. "50-F3-51-CD-B8-D8:output"
    if not uid:
        raise RuntimeError(f"SwitchAudioSource returned no uid. Raw output: {result.stdout}")

    # "50-F3-51-CD-B8-D8:output" -> "50:F3:51:CD:B8:D8"
    bt_addr = uid.removesuffix(":output").replace("-", ":")
    return bt_addr


def get_device_info(bt_addr: str) -> dict:
    """
    Query system_profiler -json, find the device matching bt_addr,
    and return its attribute dict (with '_device_name' injected).
    """
    result = run(["system_profiler", "SPBluetoothDataType", "-json"])
    data = json.loads(result.stdout)
    bt_root = data.get("SPBluetoothDataType", [{}])[0]

    # Search all sections; connected devices appear under "device_connected"
    for section_key in ("device_connected", "device_disconnected", "device_not_connected"):
        for device_dict in bt_root.get(section_key, []):
            for name, attrs in device_dict.items():
                if attrs.get("device_address", "").lower() == bt_addr.lower():
                    attrs["_device_name"] = name
                    return attrs

    raise RuntimeError(
        f"No Bluetooth device found with address {bt_addr}.\n"
        "Make sure AirPods are selected as audio output in System Settings → Sound."
    )


def parse_battery_pct(value) -> Optional[int]:
    """Extract an integer percentage from a field value like '85%' or 85."""
    if value is None or value == "":
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def read_battery(attrs: dict) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Return (left, right, case) battery percentages. None = not available."""
    left  = parse_battery_pct(attrs.get("device_batteryLevelLeft"))
    right = parse_battery_pct(attrs.get("device_batteryLevelRight"))
    case  = parse_battery_pct(attrs.get("device_batteryLevelCase"))

    # Fallback for single-value devices (older firmware)
    if left is None and right is None:
        single = parse_battery_pct(attrs.get("device_batteryLevel"))
        left = right = single

    return left, right, case


# ── Volume control ─────────────────────────────────────────────────────────────
def get_volume() -> int:
    result = run(["osascript", "-e", "output volume of (get volume settings)"])
    return int(result.stdout.strip())


def set_volume(level: int):
    run(["osascript", "-e", f"set volume output volume {level}"])


# ── Pink noise ─────────────────────────────────────────────────────────────────
def start_pink_noise() -> Optional[subprocess.Popen]:
    """Start pink noise playback. Returns the Popen object or None on failure."""
    # Try sox 'play' first, then sox directly, then afplay loop fallback
    for cmd in (
        ["play", "-q", "-n", "-c", "2", "synth", "pinknoise"],
        ["sox",  "-q", "-n", "-d", "-c", "2", "synth", "pinknoise"],
    ):
        if _cmd_exists(cmd[0]):
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log_ok(f"Pink noise started via {cmd[0]} (PID {proc.pid})")
            return proc

    # Fallback: loop a built-in system sound to keep audio active
    log_warn("sox not found — using fallback system tone. Install: brew install sox")
    script = (
        "while true; do "
        "afplay /System/Library/Sounds/Tink.aiff -v 0.3 2>/dev/null; "
        "sleep 0.4; done"
    )
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_warn(f"Fallback tone loop started (PID {proc.pid})")
    return proc


def stop_pink_noise(proc: Optional[subprocess.Popen]):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    # Belt-and-suspenders: kill any stray sox/afplay processes
    for name in ("play", "sox", "afplay"):
        subprocess.run(["killall", name], capture_output=True)


def _cmd_exists(name: str) -> bool:
    return subprocess.run(["which", name], capture_output=True).returncode == 0


# ── CSV ────────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "timestamp", "model_name", "model_number",
    "serial_case", "serial_left", "serial_right",
    "bt_address", "left_pct", "right_pct", "case_pct", "elapsed_min",
]


def open_csv(path: Path) -> Tuple[csv.DictWriter, object]:
    """
    Open CSV for appending. Write header only if the file is new/empty.
    Returns (writer, file_handle).
    """
    is_new = not path.exists() or path.stat().st_size == 0
    fh = open(path, "a", newline="")
    writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
    if is_new:
        writer.writeheader()
    return writer, fh


def write_row(writer, model_name, model_number, serial_case, serial_left,
              serial_right, bt_addr, left, right, case, elapsed_min):
    writer.writerow({
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model_name":  model_name,
        "model_number": model_number,
        "serial_case": serial_case,
        "serial_left": serial_left,
        "serial_right": serial_right,
        "bt_address":  bt_addr,
        "left_pct":    left  if left  is not None else "N/A",
        "right_pct":   right if right is not None else "N/A",
        "case_pct":    case  if case  is not None else "N/A",
        "elapsed_min": elapsed_min,
    })


# ── Display ────────────────────────────────────────────────────────────────────
def battery_bar(pct: Optional[int]) -> str:
    if pct is None:
        return "  N/A"
    filled = pct // 10
    bar = "█" * filled + "░" * (10 - filled)
    color = C.GREEN if pct > 30 else (C.YELLOW if pct > 10 else C.RED)
    return f"{color}{bar}{C.RESET} {pct:3d}%"


# ── CLI ────────────────────────────────────────────────────────────────────────
HELP_TEXT = f"""
{C.BOLD}airpods_health_test.py{C.RESET} — AirPods Battery Health Test

{C.BOLD}USAGE{C.RESET}
  python3 airpods_health_test.py [OPTIONS]
  python3 airpods_health_test.py help

{C.BOLD}OPTIONS{C.RESET}
  --serial <SN>            Case serial number (skips prompt if auto-detection fails)
  --interval <min>         Minutes between samples               (default: 5 min)
  --output-dir <dir>       Directory to write the CSV            (default: . current dir)
  --cutoff-percent <pct>   Stop when either earbud reaches this  (default: 10%)
  --volume <pct>           Playback volume during test           (default: 50%)
  --skip-ear-detection     Skip the Disable Automatic Ear Detection prompt
  --debug                  Print raw device JSON keys and values

{C.BOLD}EXAMPLES{C.RESET}
  # Basic run
  python3 airpods_health_test.py

  # Pre-fill serial, sample every 2 min, stop at 15%
  python3 airpods_health_test.py --serial DD609JF2M7 --interval 2 --cutoff-percent 15

  # Save to specific folder, custom volume
  python3 airpods_health_test.py --output-dir ~/AirPodsTests --volume 40

{C.BOLD}OUTPUT FILE{C.RESET}
  Named automatically: <camelModel>-<caseSerial>.csv
  Example:  airpodsPro2-DD609JF2M7.csv

  If the file already exists, rows are {C.BOLD}appended{C.RESET} (no duplicate header).

{C.BOLD}CSV COLUMNS{C.RESET}
  timestamp, model_name, model_number,
  serial_case, serial_left, serial_right,
  bt_address, left_pct, right_pct, case_pct, elapsed_min

{C.BOLD}REQUIREMENTS{C.RESET}
  - macOS Ventura / Sonoma / Sequoia / Tahoe
  - brew install switchaudio-osx   (device detection)
  - brew install sox               (pink noise, optional)
  - AirPods connected as audio output before running
"""


def parse_args():
    if len(sys.argv) > 1 and sys.argv[1] in ("help", "--help", "-h"):
        print(HELP_TEXT)
        sys.exit(0)

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--serial",         default="")
    parser.add_argument("--interval",       type=int, default=5)
    parser.add_argument("--output-dir",     dest="output_dir", default=".")
    parser.add_argument("--cutoff-percent", dest="cutoff_percent", type=int, default=10)
    parser.add_argument("--volume",         type=int, default=50)
    parser.add_argument("--skip-ear-detection", dest="skip_ear_detection", action="store_true", default=False)
    parser.add_argument("--debug",              action="store_true", default=False)
    return parser.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    interval_sec   = args.interval * 60
    stop_threshold = args.cutoff_percent
    target_volume  = args.volume
    output_dir     = Path(args.output_dir).expanduser().resolve()
    serial_arg     = args.serial.strip()

    # State for cleanup handler
    audio_proc    = None
    orig_volume   = None
    csv_fh        = None
    csv_path      = None

    def cleanup(signum=None, frame=None):
        print()
        log_warn("Shutting down...")
        stop_pink_noise(audio_proc)
        if orig_volume is not None:
            set_volume(orig_volume)
            log(f"Volume restored to {orig_volume}%")
        if csv_fh:
            csv_fh.flush()
            csv_fh.close()
        if csv_path and csv_path.exists():
            rows = sum(1 for _ in open(csv_path)) - 1  # subtract header
            log_ok(f"Saved {rows} row(s) → {csv_path}")
        sys.exit(0)

    signal.signal(signal.SIGINT,  cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # ── Banner ────────────────────────────────────────────────────────────────
    os.system("clear")
    print(f"{C.BOLD}{C.CYAN}")
    print("╔══════════════════════════════════════════════════════╗")
    print("║          AirPods Battery Health Test  v4            ║")
    print(f"╚══════════════════════════════════════════════════════╝{C.RESET}")
    print(f"  Interval: {C.BOLD}{args.interval} min{C.RESET}  |  "
          f"Cutoff: {C.BOLD}{stop_threshold}%{C.RESET}  |  "
          f"Volume: {C.BOLD}{target_volume}%{C.RESET}")
    print(f"  Output: {C.BOLD}{output_dir}{C.RESET}")

    # ── Prerequisites ─────────────────────────────────────────────────────────
    log_head("Prerequisites")
    for dep, hint in [
        ("system_profiler", "built into macOS"),
        ("osascript",       "built into macOS"),
        ("SwitchAudioSource", "brew install switchaudio-osx"),
    ]:
        if not _cmd_exists(dep):
            log_err(f"{dep} not found. {hint}")
            sys.exit(1)
    log_ok("switchaudio-osx present")
    if not (_cmd_exists("play") or _cmd_exists("sox")):
        log_warn("sox not found (brew install sox) — fallback tone will be used")
    else:
        log_ok("sox present — pink noise available")

    # ── Detect device ─────────────────────────────────────────────────────────
    log_head("Detecting AirPods")
    try:
        bt_addr = get_current_audio_bt_address()
        attrs   = get_device_info(bt_addr)
    except RuntimeError as e:
        log_err(str(e))
        sys.exit(1)

    device_name = attrs.get("_device_name", "AirPods")

    # ── Debug: dump raw device attributes ────────────────────────────────────
    if args.debug:
        log_head("Debug: Raw Device Attributes")
        for k, v in sorted(attrs.items()):
            print(f"  {C.CYAN}{k}{C.RESET} = {v}")
        print()

    # device_productID is a hex string e.g. "0x2024", not an integer
    product_id_raw = attrs.get("device_productID") or attrs.get("device_product_id")
    if product_id_raw is None:
        log_warn(f"device_productID not found. Available keys: {list(attrs.keys())}")
    try:
        if product_id_raw is None:
            product_id_int = None
        else:
            s = str(product_id_raw).strip()
            product_id_int = int(s, 16) if s.startswith(("0x", "0X")) else int(s, 10)
    except (ValueError, TypeError):
        product_id_int = None

    serial_case  = attrs.get("device_serialNumber",      "")
    serial_left  = attrs.get("device_serialNumberLeft",  "")
    serial_right = attrs.get("device_serialNumberRight", "")

    log_ok(f"Detected:      {C.BOLD}{device_name}{C.RESET}  ({bt_addr})")
    log_ok(f"Serial (case): {C.BOLD}{serial_case or 'not found'}{C.RESET}")
    log_ok(f"Serial (L/R):  {C.BOLD}{serial_left or '?'}{C.RESET} / {C.BOLD}{serial_right or '?'}{C.RESET}")
    log_ok(f"ProductID:     {C.BOLD}{hex(product_id_int) if product_id_int else 'not found'}{C.RESET}")

    # ── Serial override / prompt ───────────────────────────────────────────────
    serial_number = serial_arg or serial_case
    if serial_arg:
        log_warn(f"Serial overridden by --serial flag: {serial_number}")
    if not serial_number:
        log_warn("Case serial not found automatically.")
        serial_number = input("  Enter serial number (Enter for 'UNKNOWN'): ").strip() or "UNKNOWN"

    # ── Model lookup ──────────────────────────────────────────────────────────
    log_head("Identifying Model")
    model_info = lookup_model(product_id_int) if product_id_int else None

    if model_info:
        model_name, model_number, camel_name = model_info
        log_ok(f"{hex(product_id_int)} → {model_name} ({model_number})")
    else:
        model_name   = device_name
        # Use the raw hex product ID as model_number so the filename is meaningful
        model_number = str(product_id_raw) if product_id_raw is not None else "Unknown"
        camel_name   = infer_camel(device_name)
        if product_id_int:
            log_warn(f"ProductID {hex(product_id_int)} not in lookup table — using device name.")
        else:
            log_warn("ProductID not found — using device name as model.")

    # ── Initial battery check ─────────────────────────────────────────────────
    log_head("Battery Status")
    left, right, case = read_battery(attrs)
    initial_left, initial_right = left, right
    start_time = datetime.now()

    print(f"  Model:   {C.BOLD}{model_name}{C.RESET}  /  {model_number}")
    print(f"  Serial:  {C.BOLD}{serial_number}{C.RESET}")
    print(f"  BT Addr: {bt_addr}")
    print()
    print(f"  Left:  {battery_bar(left)}")
    print(f"  Right: {battery_bar(right)}")
    print(f"  Case:  {battery_bar(case)}")

    min_pct = min(v for v in (left, right) if v is not None) if any(
        v is not None for v in (left, right)) else None
    if min_pct is not None and min_pct <= stop_threshold:
        log_err(f"Battery already ≤ {stop_threshold}%. Charge AirPods and retry.")
        sys.exit(1)

    # ── Disable Ear Detection (manual step) ───────────────────────────────────
    if not args.skip_ear_detection:
        log_head("Disable Automatic Ear Detection")
        print()
        print(f"  {C.YELLOW}Please Disable Automatic Ear Detection{C.RESET} to prevent sleep.")
        print(f"  {C.CYAN}Recommended for robustness — prevents AirPods pausing when not in ear.{C.RESET}")
        print()
        print("  1. System Settings → Bluetooth")
        print("  2. Click ⓘ next to your AirPods → 'AirPods Settings...'")
        print("  3. Toggle OFF 'Automatic Ear Detection'")
        print()
        input("  Press Enter to Proceed: ")

    # ── Normalize volume ──────────────────────────────────────────────────────
    log_head(f"Normalizing Volume to {target_volume}%")
    orig_volume = get_volume()
    set_volume(target_volume)
    log_ok(f"Volume: {orig_volume}% → {target_volume}%")

    # ── CSV setup ─────────────────────────────────────────────────────────────
    log_head("Output File")
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{camel_name}-{serial_number}.csv"
    is_append = csv_path.exists() and csv_path.stat().st_size > 0
    csv_writer, csv_fh = open_csv(csv_path)
    log_ok(f"{'Appending to' if is_append else 'Created'}: {csv_path}")
    print(f"\n  {C.BOLD}Output file:{C.RESET} {csv_path}")

    # ── Start pink noise ──────────────────────────────────────────────────────
    log_head("Starting Audio (Pink Noise)")
    audio_proc = start_pink_noise()

    # ── Sampling loop ─────────────────────────────────────────────────────────
    print()
    print(f"{C.BOLD}Test running.{C.RESET}")
    print(f"  Sampling every {C.BOLD}{args.interval} min{C.RESET}  |  "
          f"Stops at {C.BOLD}{stop_threshold}%{C.RESET}  |  "
          f"Press {C.BOLD}Ctrl-C{C.RESET} to exit")
    print()

    sample_num = 0
    test_start = time.time()

    while True:
        now         = time.time()
        elapsed_min = int((now - test_start) / 60)

        # Refresh device data each cycle
        try:
            attrs       = get_device_info(bt_addr)
            left, right, case = read_battery(attrs)
        except Exception as e:
            log_warn(f"Could not refresh device data ({e}) — retrying next cycle.")
            time.sleep(interval_sec)
            continue

        write_row(csv_writer, model_name, model_number,
                  serial_case, serial_left, serial_right,
                  bt_addr, left, right, case, elapsed_min)
        csv_fh.flush()
        sample_num += 1

        # SAMPLE_LINES: number of lines printed per sample block (used for redraw)
        SAMPLE_LINES = 4  # header + Left + Right + Case + countdown

        # On the first sample, print the block fresh.
        # On subsequent samples, move cursor up to overwrite the previous block.
        if sample_num > 1:
            # Move up SAMPLE_LINES and clear each line
            sys.stdout.write((C.UP.format(1) + C.CLEAR) * SAMPLE_LINES)
            sys.stdout.flush()

        print(f"{C.BOLD}Sample #{sample_num}{C.RESET}  "
              f"+{elapsed_min} min  {datetime.now().strftime('%H:%M:%S')}")
        print(f"  Left:  {battery_bar(left)}")
        print(f"  Right: {battery_bar(right)}")
        print(f"  Case:  {battery_bar(case)}")

        # Stop condition
        earbud_vals = [v for v in (left, right) if v is not None]
        if earbud_vals and min(earbud_vals) <= stop_threshold:
            print()
            log_warn(f"Earbud at {min(earbud_vals)}% — reached {stop_threshold}% threshold. Test complete.")
            final_left, final_right = left, right
            break

        if left is None and right is None:
            log_warn("No battery data — AirPods may have disconnected. Retrying next cycle.")

        # Live countdown (overwrites same line, stays within the block)
        deadline = now + interval_sec
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            mins, secs = divmod(int(remaining), 60)
            print(f"\r  Next sample in {mins:02d}:{secs:02d}  ", end="", flush=True)
            time.sleep(1)
        print(f"\r{' ' * 30}\r", end="")

    # ── Summary ───────────────────────────────────────────────────────────────
    end_time  = datetime.now()
    total_min = int((time.time() - test_start) / 60)

    # Guard: if loop exited via Ctrl-C, final_left/right may not be set
    try:    final_left, final_right
    except: final_left, final_right = left, right

    def fmt_pct(v): return f"{v}%" if v is not None else "N/A"

    print()
    print(f"{C.BOLD}{C.GREEN}══════════ Test Complete ══════════{C.RESET}")
    print()
    print(f"  {'Start:':10} {start_time.strftime('%H:%M:%S')}   "
          f"L: {C.BOLD}{fmt_pct(initial_left)}{C.RESET}   "
          f"R: {C.BOLD}{fmt_pct(initial_right)}{C.RESET}")
    print(f"  {'End:':10} {end_time.strftime('%H:%M:%S')}   "
          f"L: {C.BOLD}{fmt_pct(final_left)}{C.RESET}   "
          f"R: {C.BOLD}{fmt_pct(final_right)}{C.RESET}")
    print()
    print(f"  {'Score:':10} {C.BOLD}{total_min} min{C.RESET} to reach {stop_threshold}% cutoff")
    print(f"  {'File:':10} {csv_path}")
    print()

    # Explicit cleanup (also handles non-signal exit)
    cleanup()


if __name__ == "__main__":
    main()
