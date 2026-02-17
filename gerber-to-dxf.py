#!/usr/bin/env python3
"""Create a DXF with board outline and mounting holes from Gerber/Excellon files."""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path


def parse_gerber_coord(value: str, int_digits: int, dec_digits: int, zero_suppression: str) -> float:
    sign = -1.0 if value.startswith("-") else 1.0
    digits = value[1:] if value[:1] in "+-" else value
    if "." in digits:
        return sign * float(digits)

    total = int_digits + dec_digits
    if len(digits) < total:
        if zero_suppression == "L":
            digits = digits.rjust(total, "0")
        elif zero_suppression == "T":
            digits = digits.ljust(total, "0")
        else:
            digits = digits.rjust(total, "0")
    elif len(digits) > total:
        digits = digits[-total:]
    return sign * (int(digits) / (10**dec_digits))


def detect_input_files(cwd: Path) -> tuple[Path, Path | None, Path | None]:
    edge_candidates = sorted(cwd.glob("*-Edge_Cuts.gbr"))
    if not edge_candidates:
        for gerber in sorted(cwd.glob("*.gbr")):
            text = gerber.read_text(encoding="utf-8", errors="ignore")
            if "FileFunction,Profile" in text or "AperFunction,Profile" in text:
                edge_candidates.append(gerber)
                break
    if not edge_candidates:
        raise FileNotFoundError("No edge-cuts/profile Gerber found.")

    pth = next(iter(sorted(cwd.glob("*-PTH.drl"))), None)
    npth = next(iter(sorted(cwd.glob("*-NPTH.drl"))), None)
    return edge_candidates[0], pth, npth


def parse_gerber_outline(path: Path) -> list[tuple]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    x_int = 4
    x_dec = 6
    y_int = 4
    y_dec = 6
    zero_suppression = "L"
    units = "MM"

    mode = "LINEAR"  # LINEAR, CW, CCW
    cur_x = None
    cur_y = None
    entities: list[tuple] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("G04"):
            continue
        if line.endswith("*"):
            line = line[:-1]

        if line.startswith("%") and line.endswith("%"):
            param = line[1:-1].upper()
            fs_match = re.match(r"^FS([LTD])A?X(\d)(\d)Y(\d)(\d)$", param)
            if fs_match:
                zero_suppression = fs_match.group(1)
                x_int, x_dec, y_int, y_dec = map(int, fs_match.groups()[1:])
                continue
            if param == "MOIN":
                units = "IN"
                continue
            if param == "MOMM":
                units = "MM"
                continue
            continue

        upper = line.upper()
        for gcode in re.findall(r"G0?([123])", upper):
            if gcode == "1":
                mode = "LINEAR"
            elif gcode == "2":
                mode = "CW"
            elif gcode == "3":
                mode = "CCW"

        dcode = None
        for m in re.finditer(r"D0?(\d+)", upper):
            val = int(m.group(1))
            if val in (1, 2, 3):
                dcode = val

        def axis(letter: str) -> str | None:
            m = re.search(rf"{letter}([+\-]?\d*\.?\d+)", line, flags=re.IGNORECASE)
            return m.group(1) if m else None

        x_raw = axis("X")
        y_raw = axis("Y")
        i_raw = axis("I")
        j_raw = axis("J")

        next_x = cur_x
        next_y = cur_y
        if x_raw is not None:
            next_x = parse_gerber_coord(x_raw, x_int, x_dec, zero_suppression)
        if y_raw is not None:
            next_y = parse_gerber_coord(y_raw, y_int, y_dec, zero_suppression)

        if dcode == 2:
            cur_x, cur_y = next_x, next_y
            continue

        if dcode == 1 and None not in (cur_x, cur_y, next_x, next_y):
            if mode == "LINEAR":
                entities.append(("LINE", cur_x, cur_y, next_x, next_y))
            else:
                i_off = parse_gerber_coord(i_raw, x_int, x_dec, zero_suppression) if i_raw else 0.0
                j_off = parse_gerber_coord(j_raw, y_int, y_dec, zero_suppression) if j_raw else 0.0
                cx = cur_x + i_off
                cy = cur_y + j_off
                radius = math.hypot(cur_x - cx, cur_y - cy)
                start_angle = math.degrees(math.atan2(cur_y - cy, cur_x - cx)) % 360.0
                end_angle = math.degrees(math.atan2(next_y - cy, next_x - cx)) % 360.0
                if mode == "CW":
                    start_angle, end_angle = end_angle, start_angle
                entities.append(("ARC", cx, cy, radius, start_angle, end_angle))
            cur_x, cur_y = next_x, next_y

    factor = 25.4 if units == "IN" else 1.0
    converted: list[tuple] = []
    for entity in entities:
        if entity[0] == "LINE":
            _, x1, y1, x2, y2 = entity
            converted.append(("LINE", x1 * factor, y1 * factor, x2 * factor, y2 * factor))
        else:
            _, cx, cy, r, a0, a1 = entity
            converted.append(("ARC", cx * factor, cy * factor, r * factor, a0, a1))
    return converted


def parse_drill_file(
    path: Path, min_hole_size_mm: float, max_hole_size_mm: float | None
) -> list[tuple[float, float, float]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    units = "IN"
    tool_diameter: dict[int, float] = {}
    current_tool: int | None = None
    current_x: float | None = None
    current_y: float | None = None
    holes: list[tuple[float, float, float]] = []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        upper = line.upper()

        if upper.startswith("INCH"):
            units = "IN"
            continue
        if upper.startswith("METRIC"):
            units = "MM"
            continue

        m = re.match(r"^T(\d+)C([+\-]?\d*\.?\d+)$", upper)
        if m:
            tool_diameter[int(m.group(1))] = float(m.group(2))
            continue

        m = re.match(r"^T(\d+)$", upper)
        if m:
            current_tool = int(m.group(1))
            continue

        if upper in {"M48", "%", "G90", "G05", "M30"} or upper.startswith("FMAT"):
            continue

        if upper.startswith(("X", "Y")):
            mx = re.search(r"X([+\-]?\d*\.?\d+)", line, flags=re.IGNORECASE)
            my = re.search(r"Y([+\-]?\d*\.?\d+)", line, flags=re.IGNORECASE)
            if mx:
                current_x = float(mx.group(1))
            if my:
                current_y = float(my.group(1))
            if None in (current_tool, current_x, current_y):
                continue
            if current_tool not in tool_diameter:
                continue

            factor = 25.4 if units == "IN" else 1.0
            diameter_mm = tool_diameter[current_tool] * factor
            if diameter_mm < min_hole_size_mm:
                continue
            if max_hole_size_mm is not None and diameter_mm > max_hole_size_mm:
                continue
            holes.append((current_x * factor, current_y * factor, diameter_mm))

    return holes


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a DXF with board outline and mounting holes from Gerber/Excellon files."
    )
    parser.add_argument(
        "--min",
        dest="min_hole_size",
        type=float,
        default=3.0,
        help="Minimum drill diameter in mm to include as a mounting hole (default: 3.0).",
    )
    parser.add_argument(
        "--max",
        dest="max_hole_size",
        type=float,
        default=None,
        help="Maximum drill diameter in mm to include as a mounting hole (default: no maximum).",
    )
    return parser


def parse_args(argv: list[str], parser: argparse.ArgumentParser | None = None) -> argparse.Namespace:
    parser = parser or build_arg_parser()
    return parser.parse_args(argv)


def write_dxf(path: Path, outline: list[tuple], holes: list[tuple[float, float, float]]) -> None:
    def emit(*args: object) -> str:
        return "".join(f"{a}\n" for a in args)

    parts = []
    parts.append(
        emit(
            0,
            "SECTION",
            2,
            "HEADER",
            9,
            "$INSUNITS",
            70,
            4,
            0,
            "ENDSEC",
            0,
            "SECTION",
            2,
            "TABLES",
            0,
            "TABLE",
            2,
            "LAYER",
            70,
            2,
            0,
            "LAYER",
            2,
            "OUTLINE",
            70,
            0,
            62,
            7,
            6,
            "CONTINUOUS",
            0,
            "LAYER",
            2,
            "MOUNTING_HOLES",
            70,
            0,
            62,
            1,
            6,
            "CONTINUOUS",
            0,
            "ENDTAB",
            0,
            "ENDSEC",
            0,
            "SECTION",
            2,
            "ENTITIES",
        )
    )

    for entity in outline:
        if entity[0] == "LINE":
            _, x1, y1, x2, y2 = entity
            parts.append(
                emit(
                    0,
                    "LINE",
                    8,
                    "OUTLINE",
                    10,
                    f"{x1:.6f}",
                    20,
                    f"{y1:.6f}",
                    30,
                    "0.0",
                    11,
                    f"{x2:.6f}",
                    21,
                    f"{y2:.6f}",
                    31,
                    "0.0",
                )
            )
        else:
            _, cx, cy, radius, start_angle, end_angle = entity
            parts.append(
                emit(
                    0,
                    "ARC",
                    8,
                    "OUTLINE",
                    10,
                    f"{cx:.6f}",
                    20,
                    f"{cy:.6f}",
                    30,
                    "0.0",
                    40,
                    f"{radius:.6f}",
                    50,
                    f"{start_angle:.6f}",
                    51,
                    f"{end_angle:.6f}",
                )
            )

    for x, y, diameter in holes:
        parts.append(
            emit(
                0,
                "CIRCLE",
                8,
                "MOUNTING_HOLES",
                10,
                f"{x:.6f}",
                20,
                f"{y:.6f}",
                30,
                "0.0",
                40,
                f"{diameter / 2.0:.6f}",
            )
        )

    parts.append(emit(0, "ENDSEC", 0, "EOF"))
    path.write_text("".join(parts), encoding="ascii")


def main() -> int:
    parser = build_arg_parser()
    args = parse_args(sys.argv[1:], parser)
    if args.min_hole_size < 0:
        print("Error: --min must be >= 0.", file=sys.stderr)
        return 2
    if args.max_hole_size is not None and args.max_hole_size < 0:
        print("Error: --max must be >= 0.", file=sys.stderr)
        return 2
    if args.max_hole_size is not None and args.max_hole_size < args.min_hole_size:
        print("Error: --max must be >= --min.", file=sys.stderr)
        return 2

    print("Command-line help:")
    print(parser.format_help().rstrip())

    cwd = Path.cwd()
    try:
        edge_file, pth_file, npth_file = detect_input_files(cwd)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    outline = parse_gerber_outline(edge_file)
    holes: list[tuple[float, float, float]] = []
    if pth_file:
        holes.extend(parse_drill_file(pth_file, args.min_hole_size, args.max_hole_size))
    if npth_file:
        holes.extend(parse_drill_file(npth_file, args.min_hole_size, args.max_hole_size))

    project = re.sub(r"-Edge_Cuts\.gbr$", "", edge_file.name, flags=re.IGNORECASE)
    if project == edge_file.name:
        project = edge_file.stem
    output = cwd / f"{project}-outline-mounting-holes.dxf"
    write_dxf(output, outline, holes)

    diameters = sorted({round(d, 6) for _, _, d in holes})
    diam_text = ", ".join(f"{d:.4f}" for d in diameters) if diameters else "none"

    print(f"Output file: {output.name}")
    print(f"Outline entities: {len(outline)}")
    if args.max_hole_size is None:
        print(f"Hole count (>= {args.min_hole_size:.4f} mm): {len(holes)}")
    else:
        print(f"Hole count ({args.min_hole_size:.4f} to {args.max_hole_size:.4f} mm): {len(holes)}")
    print(f"Diameters used (mm): {diam_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
