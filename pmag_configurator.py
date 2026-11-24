#!/usr/bin/env python3
"""
EICommander P-mag Configurator  (Python port of "EIC P-mag Configurator.xlsm")

Reads and writes ignition timing settings on an E-MAG/P-MAG electronic
magneto over an RS-232 serial link, and keeps a CSV log of every read/write
(the equivalent of the workbook's "Log" sheet).

Requirements
    Python 3.8+
    pip install pyserial          (only needed to talk to real hardware)

Examples
    python pmag_configurator.py                      # interactive menu
    python pmag_configurator.py engines              # list engine configurations
    python pmag_configurator.py ports                # list serial ports
    python pmag_configurator.py show --engine 6      # compute settings only
    python pmag_configurator.py show --timing 23.8 --max-adv 28.0 --rpm 3072
    python pmag_configurator.py read  --port COM8
    python pmag_configurator.py write --port COM8 --engine 6 --notes "Annual"
    python pmag_configurator.py write --port COM8 --engine 6 --dry-run
    python pmag_configurator.py read  --simulate     # no hardware needed

Mapping from the workbook
    Constants sheet       -> ENGINES, HIGH_POWER_TIMING_OPTIONS, ...
    Configurator formulas -> compute_config()
    Read_Config macro     -> read_config()
    Write_Config macro    -> write_config()
    Log sheet             -> pmag_log.csv (next to this script)
    P-mag Commands sheet  -> PMag class / encode_* / decode_* functions

NOTE: In the workbook the macros were still a prototype: `Skip = 1` was
hard-coded, so Read_Config never read from the port (it used the fixed values
-14 / 252 / 12) and Write_Config never sent any commands. This script does
the real serial I/O. Use --simulate to reproduce the old canned behaviour.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# Constants (from the "Constants" sheet)
# --------------------------------------------------------------------------

# The P-mag advances 1.4 deg beyond its MaxAdv setting, and the "takeoff /
# high power timing" is the constellation shift (AdvShft) plus 26.6 deg.
TIMING_BASE = 26.6          # Takeoff timing = AdvShft + 26.6
MAX_ADV_OFFSET = 1.4        # Actual max advance = MaxAdv setting + 1.4
RPM_BLOCK = 256             # RPM limit is stored in 256-rpm blocks

# Serial settings (Constants!B26:B29)
BAUD_RATE = 9600
DATA_BITS = 8
STOP_BITS = 1
PARITY = "N"

CUSTOM_ENGINE = 10          # "Create a Custom Configuration"


@dataclass(frozen=True)
class Engine:
    number: int
    name: str
    takeoff_timing: Optional[float]   # deg BTDC (Config_Table col 3, "Absolute Shift")
    max_advance: Optional[float]      # deg BTDC (Config_Table col 4, "Max Adv")
    rpm_limit: Optional[int]          # rpm      (Config_Table col 5, "RPM")


# Engine_Choice / Config_Table  (Constants!A2:B12 and F2:J12).
# Engine 1 is the blank first entry in the workbook's drop-down list.
ENGINES = {
    2:  Engine(2,  "Low Compression O-320 150 HP - Break-in (Parallel Valve)",                  25.2, 25.2, 3072),
    3:  Engine(3,  "Low Compression O-320 150 HP (Parallel Valve)",                             25.2, 32.2, 3072),
    4:  Engine(4,  "Standard Compression (I)O-320 160 HP - Break-in (Parallel Valve)",          25.2, 25.2, 3072),
    5:  Engine(5,  "Standard Compression (I)O-320 160 HP (Parallel Valve)",                     25.2, 32.2, 3072),
    6:  Engine(6,  "Standard Compression (I)O-360 180 HP - Break-in (Parallel Valve)",          25.2, 25.2, 3072),
    7:  Engine(7,  "Standard Compression (I)O-360 180 HP (Parallel Valve)",                     25.2, 32.2, 3072),
    8:  Engine(8,  "Standard Compression (I)O-360/390 200 to 210 HP - Break-in (Angle Valve)",  19.6, 21.0, 3072),
    9:  Engine(9,  "Standard Compression (I)O-360/390 200 to 210 HP (Angle Valve)",             21.0, 25.2, 3072),
    10: Engine(10, "Create a Custom Configuration",                                            None, None, None),
    # NOTE: the workbook lists AdvShft 4.2 for the defaults, which would be
    # 30.8 deg, but its "Absolute Shift" cell is 26.6. Kept as in the workbook.
    11: Engine(11, "P-mag Default Settings",                                                    26.6, 39.2, 3328),
}

# Custom-configuration drop-down choices (High_Power_Timing, Max_Advance,
# RPM_Limit ranges). Timing/advance options are 1.4-degree steps.
HIGH_POWER_TIMING_OPTIONS = [round(15.4 + 1.4 * i, 1) for i in range(17)]  # 15.4 .. 37.8
MAX_ADVANCE_OPTIONS = [round(15.4 + 1.4 * i, 1) for i in range(23)]        # 15.4 .. 46.2
RPM_LIMIT_OPTIONS = [2816, 3072, 3328, 3584]

LOG_FILE = Path(__file__).with_name("pmag_log.csv")
LOG_COLUMNS = [
    "Date & Time", "Action", "Com Port", "Engine", "Firmware Version",
    "Takeoff/High Power Timing", "ADVShft P-mag Setting",
    "Low Power / Cruise Max Advance", "MaxADV P-mag Setting",
    "# RPM Blocks", "Max RPM", "Notes",
]


# --------------------------------------------------------------------------
# Configuration math (the Configurator sheet formulas C22:C24)
# --------------------------------------------------------------------------

@dataclass
class Config:
    takeoff_timing: float     # deg - what the pilot thinks in
    max_advance: float        # deg
    rpm_limit: int            # rpm

    # Values actually programmed into the P-mag
    @property
    def adv_shift(self) -> float:
        return round(self.takeoff_timing - TIMING_BASE, 1)

    @property
    def max_adv_setting(self) -> float:
        return round(self.max_advance - MAX_ADV_OFFSET, 1)

    @property
    def rpm_blocks(self) -> int:
        return round(self.rpm_limit / RPM_BLOCK)

    def summary(self) -> str:
        return (
            f"  Takeoff/High Power Timing      : {self.takeoff_timing:5.1f} deg"
            f"   (P-mag AdvShft {self.adv_shift:+.1f})\n"
            f"  Low Power / Cruise Max Advance : {self.max_advance:5.1f} deg"
            f"   (P-mag MaxAdv  {self.max_adv_setting:.1f})\n"
            f"  RPM Limiter                    : {self.rpm_limit:5d} rpm"
            f"   ({self.rpm_blocks} x {RPM_BLOCK})"
        )


def _pick(value: float, options: list, label: str):
    """Match a user value to one of the drop-down options (like the workbook)."""
    for opt in options:
        if abs(opt - value) < 0.05:
            return opt
    raise ValueError(f"{label} {value} is not an allowed choice. Options: {options}")


def compute_config(engine_number: int,
                   timing: Optional[float] = None,
                   max_adv: Optional[float] = None,
                   rpm: Optional[int] = None) -> Config:
    """Equivalent of:
         =IF(B17=10, VLOOKUP(custom, <option table>, 2), VLOOKUP(B17, Config_Table, n))
    """
    if engine_number not in ENGINES:
        raise ValueError(f"Unknown engine number {engine_number}. Valid: {sorted(ENGINES)}")
    if engine_number == CUSTOM_ENGINE:
        if timing is None or max_adv is None or rpm is None:
            raise ValueError("Custom configuration needs --timing, --max-adv and --rpm")
        return Config(
            _pick(timing, HIGH_POWER_TIMING_OPTIONS, "Takeoff timing"),
            _pick(max_adv, MAX_ADVANCE_OPTIONS, "Max advance"),
            int(_pick(rpm, RPM_LIMIT_OPTIONS, "RPM limit")),
        )
    e = ENGINES[engine_number]
    return Config(e.takeoff_timing, e.max_advance, e.rpm_limit)


# --------------------------------------------------------------------------
# Wire encoding (the "P-mag Commands" sheet)
#
# The workbook has two references that disagree:
#   * "Per Ed":  /*  /C-70  /N280  /M3072  /W   -> tenths of a degree, raw rpm
#   * The firmware command list: /C and /N in units of 1.406 deg, /M in x256
# The Read_Config macro decoded /c and /n as tenths of a degree, matching
# Ed's format, so that is used here. If your firmware differs, change only
# these four functions.
# --------------------------------------------------------------------------

def encode_adv_shift(deg: float) -> str:
    return f"/C{round(deg * 10)}"


def encode_max_adv(deg: float) -> str:
    return f"/N{round(deg * 10)}"


def encode_rpm_limit(rpm: int) -> str:
    return f"/M{int(rpm)}"


def decode_tenths(text: str) -> float:
    return int(text) / 10


def decode_rpm_limit(text: str) -> int:
    """The macro treated /m as 256-rpm blocks ("12" -> 3072); Ed's write
    format uses raw rpm. Accept either: small numbers are blocks."""
    n = int(text)
    return n * RPM_BLOCK if n < 100 else n


# --------------------------------------------------------------------------
# Serial communication
# --------------------------------------------------------------------------

class PMagError(Exception):
    pass


class PMag:
    """Talks to one P-mag over RS-232 (9600 8N1, commands end in CR)."""

    def __init__(self, port: str, timeout: float = 1.0):
        try:
            import serial  # pyserial
        except ImportError:
            raise PMagError("pyserial is not installed. Run:  pip install pyserial")
        self.port = port
        try:
            self.ser = serial.Serial(port, BAUD_RATE, bytesize=DATA_BITS,
                                     parity=PARITY, stopbits=STOP_BITS, timeout=timeout)
        except serial.SerialException as exc:
            raise PMagError(f"Can't open serial port {port}: {exc}")

    def close(self):
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def send(self, cmd: str):
        self.ser.write((cmd + "\r").encode("ascii"))
        self.ser.flush()

    def _readline(self) -> str:
        raw = self.ser.read_until(b"\r")
        return raw.decode("ascii", errors="replace").strip()

    def query(self, cmd: str) -> str:
        """Send a command, return the reply line (skipping the echo)."""
        self.ser.reset_input_buffer()
        self.send(cmd)
        for _ in range(3):
            line = self._readline()
            if not line:
                break
            if line.strip() == cmd:        # the P-mag echoes commands
                continue
            return line
        raise PMagError(f"No response to {cmd!r} on {self.port}")

    def wake(self):
        self.send("/*")                    # get the P-mag's attention
        time.sleep(1.0)
        self.ser.reset_input_buffer()


class SimulatedPMag:
    """Stand-in that answers like the workbook's Skip=1 placeholders."""

    def __init__(self, port: str = "SIM", **_):
        self.port = port
        self.state = {"/x": "0000", "/?": "Unknown", "/c": "-14", "/n": "252", "/m": "12"}
        self.sent = []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def send(self, cmd: str):
        self.sent.append(cmd)
        m = re.fullmatch(r"/([CNM])(-?\d+)", cmd)
        if m:
            self.state["/" + m.group(1).lower()] = m.group(2)

    def query(self, cmd: str) -> str:
        self.sent.append(cmd)
        return self.state[cmd]

    def wake(self):
        self.send("/*")


def _digits(text: str, allow_minus: bool = False) -> str:
    """Keep only digits (and '-'), like the macro's character filter."""
    keep = "0123456789" + ("-" if allow_minus else "")
    out = "".join(ch for ch in text if ch in keep)
    if not out or out == "-":
        raise PMagError(f"Unexpected reply {text!r}")
    return out


@dataclass
class Reading:
    firmware: str
    config: Config
    raw_shift: float
    raw_max_adv: float
    rpm_blocks: int


def get_rpm(pm) -> int:
    return int(_digits(pm.query("/x")))


def get_firmware(pm) -> str:
    try:
        reply = pm.query("/?")
    except PMagError:
        return "Unknown"
    fw = "".join(ch for ch in reply if ch.isdigit() or ch == "-")
    return fw or reply or "Unknown"


def read_settings(pm) -> Reading:
    shift = decode_tenths(_digits(pm.query("/c"), allow_minus=True))
    max_adv = decode_tenths(_digits(pm.query("/n")))
    rpm_limit = decode_rpm_limit(_digits(pm.query("/m")))
    firmware = get_firmware(pm)
    cfg = Config(round(TIMING_BASE + shift, 1), round(max_adv + MAX_ADV_OFFSET, 1), rpm_limit)
    return Reading(firmware, cfg, shift, max_adv, round(rpm_limit / RPM_BLOCK))


def ensure_engine_stopped(pm):
    rpm = get_rpm(pm)
    if rpm != 0:
        raise PMagError(f"Engine must not be running! (P-mag reports {rpm} rpm)")


# --------------------------------------------------------------------------
# Log (the "Log" sheet)
# --------------------------------------------------------------------------

def append_log(action: str, port: str, engine: str, firmware: str,
               cfg: Config, notes: str, log_file: Path = LOG_FILE):
    new = not log_file.exists()
    with log_file.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_COLUMNS)
        w.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, port, engine, firmware,
            f"{cfg.takeoff_timing:.1f}", f"{cfg.adv_shift:.1f}",
            f"{cfg.max_advance:.1f}", f"{cfg.max_adv_setting:.1f}",
            cfg.rpm_blocks, cfg.rpm_blocks * RPM_BLOCK, notes,
        ])


# --------------------------------------------------------------------------
# High-level operations (the two buttons)
# --------------------------------------------------------------------------

def open_pmag(port: Optional[str], simulate: bool):
    if simulate:
        return SimulatedPMag(port or "SIM")
    if not port:
        raise PMagError("A serial port is required (e.g. --port COM8). See: ports")
    return PMag(port)


def read_config(port: Optional[str], notes: str = "", simulate: bool = False,
                log: bool = True) -> Reading:
    """Read_Config: get firmware, AdvShft, MaxAdv and RPM limit from a P-mag."""
    with open_pmag(port, simulate) as pm:
        pm.wake()
        ensure_engine_stopped(pm)
        reading = read_settings(pm)
    print(f"Current configuration ({pm.port})")
    print(f"  Firmware Version               : {reading.firmware}")
    print(reading.config.summary())
    if log:
        append_log("Read", pm.port, "N/A", reading.firmware, reading.config, notes)
    return reading


def write_config(port: Optional[str], engine_number: int, cfg: Config,
                 notes: str = "", simulate: bool = False, dry_run: bool = False,
                 assume_yes: bool = False) -> bool:
    """Write_Config: program AdvShft, MaxAdv and RPM limit, then save to flash."""
    engine_name = ENGINES[engine_number].name
    commands = [encode_adv_shift(cfg.adv_shift),
                encode_max_adv(cfg.max_adv_setting),
                encode_rpm_limit(cfg.rpm_limit),
                "/W"]

    print(f"Engine: {engine_name}")
    print(cfg.summary())
    print("Commands: " + "  ".join(commands))
    if dry_run:
        print("Dry run - nothing sent.")
        return True

    if not assume_yes and not simulate:
        ans = input(f"Write these settings to the P-mag on {port}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Cancelled.")
            return False

    with open_pmag(port, simulate) as pm:
        pm.wake()
        ensure_engine_stopped(pm)
        firmware = get_firmware(pm)
        for cmd in commands:
            pm.send(cmd)
            time.sleep(0 if simulate else 0.3)
            if isinstance(pm, PMag):
                pm.ser.reset_input_buffer()     # discard echo
        # Verify by reading back
        back = read_settings(pm)

    ok = (abs(back.config.takeoff_timing - cfg.takeoff_timing) < 0.05
          and abs(back.config.max_advance - cfg.max_advance) < 0.05
          and back.config.rpm_limit == cfg.rpm_limit)
    append_log("Write", pm.port, engine_name, firmware, cfg, notes)
    if ok:
        print("Written and verified.")
    else:
        print("WARNING: read-back does not match what was written:")
        print(back.config.summary())
    return ok


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def list_engines():
    for e in ENGINES.values():
        if e.number == CUSTOM_ENGINE:
            print(f"{e.number:3d}  {e.name}  (use --timing/--max-adv/--rpm)")
        else:
            print(f"{e.number:3d}  {e.name}\n"
                  f"       timing {e.takeoff_timing:.1f}  max adv {e.max_advance:.1f}"
                  f"  rpm {e.rpm_limit}")
    print("\nCustom choices:")
    print("  --timing  ", HIGH_POWER_TIMING_OPTIONS)
    print("  --max-adv ", MAX_ADVANCE_OPTIONS)
    print("  --rpm     ", RPM_LIMIT_OPTIONS)


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        print("pyserial is not installed. Run:  pip install pyserial")
        return
    ports = list(lp.comports())
    if not ports:
        print("No serial ports found.")
    for p in ports:
        print(f"{p.device:8s} {p.description}")


def _ask_choice(prompt: str, options: list):
    for i, opt in enumerate(options, 1):
        print(f"  {i:2d}) {opt}")
    while True:
        s = input(f"{prompt} [1-{len(options)}]: ").strip()
        if s.isdigit() and 1 <= int(s) <= len(options):
            return options[int(s) - 1]


def _ask_engine() -> int:
    """Engine menu that uses the same numbers as --engine."""
    for e in ENGINES.values():
        print(f"  {e.number:2d}) {e.name}")
    while True:
        s = input(f"Select engine [{min(ENGINES)}-{max(ENGINES)}]: ").strip()
        if s.isdigit() and int(s) in ENGINES:
            return int(s)


def interactive():
    print("EICommander P-mag Configurator\n")
    port = input("COM port (e.g. COM8, blank to simulate): ").strip().lstrip("﻿") or None
    simulate = port is None
    while True:
        print("\n1) Read current configuration\n2) Write configuration\n3) Quit")
        choice = input("> ").strip()
        try:
            if choice == "1":
                notes = input("Notes (optional): ")
                read_config(port, notes, simulate)
            elif choice == "2":
                number = _ask_engine()
                if number == CUSTOM_ENGINE:
                    cfg = compute_config(number,
                                         _ask_choice("Takeoff timing", HIGH_POWER_TIMING_OPTIONS),
                                         _ask_choice("Max advance", MAX_ADVANCE_OPTIONS),
                                         _ask_choice("RPM limit", RPM_LIMIT_OPTIONS))
                else:
                    cfg = compute_config(number)
                notes = input("Notes (optional): ")
                write_config(port, number, cfg, notes, simulate)
            elif choice == "3":
                return
        except (PMagError, ValueError) as exc:
            print(f"Error: {exc}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="EICommander P-mag Configurator")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("engines", help="list engine configurations")
    sub.add_parser("ports", help="list serial ports")

    def add_cfg_args(p):
        p.add_argument("--engine", type=int, default=CUSTOM_ENGINE,
                       help="engine number (see 'engines'); default custom")
        p.add_argument("--timing", type=float, help="custom takeoff/high power timing (deg)")
        p.add_argument("--max-adv", type=float, help="custom cruise max advance (deg)")
        p.add_argument("--rpm", type=int, help="custom RPM limit")

    p_show = sub.add_parser("show", help="compute settings without touching hardware")
    add_cfg_args(p_show)

    p_read = sub.add_parser("read", help="read the current configuration")
    p_read.add_argument("--port")
    p_read.add_argument("--notes", default="")
    p_read.add_argument("--simulate", action="store_true")
    p_read.add_argument("--no-log", action="store_true")

    p_write = sub.add_parser("write", help="write a configuration to the P-mag")
    add_cfg_args(p_write)
    p_write.add_argument("--port")
    p_write.add_argument("--notes", default="")
    p_write.add_argument("--simulate", action="store_true")
    p_write.add_argument("--dry-run", action="store_true", help="show commands only")
    p_write.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    args = ap.parse_args(argv)
    try:
        if args.cmd is None:
            interactive()
        elif args.cmd == "engines":
            list_engines()
        elif args.cmd == "ports":
            list_ports()
        elif args.cmd == "show":
            cfg = compute_config(args.engine, args.timing, args.max_adv, args.rpm)
            print(f"Engine: {ENGINES[args.engine].name}")
            print(cfg.summary())
        elif args.cmd == "read":
            read_config(args.port, args.notes, args.simulate, log=not args.no_log)
        elif args.cmd == "write":
            cfg = compute_config(args.engine, args.timing, args.max_adv, args.rpm)
            ok = write_config(args.port, args.engine, cfg, args.notes,
                              args.simulate, args.dry_run, args.yes)
            return 0 if ok else 1
    except (PMagError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
