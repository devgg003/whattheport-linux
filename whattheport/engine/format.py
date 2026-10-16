"""Format utilities matching WhatThePort's formatting and styling rules."""
from typing import Tuple

MEGABYTE = 1024 * 1024
GIGABYTE = 1024 * 1024 * 1024

PALETTE_PORTS = [
    (0x126B8D, 0x6EC7ED),  # sky
    (0x704CB0, 0xB599F0),  # lavender
    (0xA23682, 0xEB9CD4),  # pink
    (0x405CBC, 0x7D9CF2),  # periwinkle
    (0x096D75, 0x7DDBE0),  # cyan
    (0x87449E, 0xD9A3F2),  # lilac
    (0x4C627D, 0xABC2E0),  # slate
]

PALETTE_AMBER = (0x966000, 0xFFB224)
PALETTE_RED = (0xC52D24, 0xFF453A)
PALETTE_SOFT_RED = (0xBA3028, 0xFF6961)

def port_color(index: int, dark: bool = True) -> str:
    pair = PALETTE_PORTS[index % len(PALETTE_PORTS)]
    val = pair[1] if dark else pair[0]
    return f"#{val:06x}"

def bytes_parts(value: int) -> Tuple[str, str]:
    mb = value / MEGABYTE
    if mb >= 1024:
        return f"{mb / 1024:.2f}", "GB"
    return f"{mb:.0f}", "MB"

def bytes_str(value: int) -> str:
    num, unit = bytes_parts(value)
    return f"{num} {unit}"

def total_parts(value: int) -> Tuple[str, str]:
    mb = value / MEGABYTE
    if mb >= 1024:
        gb = mb / 1024
        text = f"{gb:.1f}"
        if text.endswith(".0"):
            text = text[:-2]
        return text, "GB"
    return f"{mb:.0f}", "MB"

def total_str(value: int) -> str:
    num, unit = total_parts(value)
    return f"{num} {unit}"

def duration(seconds: float) -> str:
    minutes = int(seconds / 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        rest = minutes % 60
        return f"{hours}h" if rest == 0 else f"{hours}h {rest}m"
    days = hours // 24
    return f"{days}d"

def percent(val: float) -> str:
    if val <= 0:
        return "0%"
    if val < 0.1:
        return "<0.1%"
    if val < 10:
        return f"{val:.1f}%"
    return f"{round(val):.0f}%"
