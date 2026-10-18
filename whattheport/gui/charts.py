"""Drawing widgets for the memory bar, sparklines, and history charts.

The memory bar paints with GTK's snapshot API so it still shows when the
PyGObject Cairo bridge (python3-gi-cairo) is missing. Sparklines and the
detail chart still use Cairo.
"""
import cairo  # noqa: F401 - required by PyGObject for cairo.Context foreign struct converter
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, Graphene, Gsk
from typing import List, Optional, Tuple
from whattheport.engine.format import PALETTE_AMBER

def hex_to_rgb(hex_code: str) -> Tuple[float, float, float]:
    hex_code = hex_code.lstrip("#")
    return tuple(int(hex_code[i:i+2], 16) / 255.0 for i in (0, 2, 4))

class MemoryBar(Gtk.Widget):
    """Segmented horizontal memory bar: servers by color, other apps, free RAM."""
    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self.set_size_request(-1, 10)
        self.segments: List[Tuple[float, str]] = []  # [(fraction, hex_color)]
        self.other_fraction: float = 0.0
        self.free_fraction: float = 1.0

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            return 10, 10, -1, -1
        return 40, 40, -1, -1

    def set_data(self, segments: List[Tuple[float, str]], other_fraction: float):
        self.segments = segments
        self.other_fraction = other_fraction
        server_total = sum(s[0] for s in segments)
        self.free_fraction = max(0.0, 1.0 - (server_total + other_fraction))
        self.queue_draw()

    def do_snapshot(self, snapshot):
        width = float(self.get_width())
        height = float(self.get_height())
        if width <= 0 or height <= 0:
            return

        radius = min(3.0, height / 2.0)
        bounds = Graphene.Rect().init(0, 0, width, height)
        clip = Gsk.RoundedRect().init_from_rect(bounds, radius)
        snapshot.push_rounded_clip(clip)

        def color_of(r, g, b, a):
            color = Gdk.RGBA()
            color.red, color.green, color.blue, color.alpha = r, g, b, a
            return color

        def fill(x, seg_w, color):
            if seg_w <= 0:
                return
            snapshot.append_color(color, Graphene.Rect().init(x, 0, seg_w, height))

        # Full-width track is free RAM. It has to stay visible on the dark panel,
        # otherwise a half-used machine looks like a bar that stops halfway.
        fill(0, width, color_of(1, 1, 1, 0.16))

        slices = []
        for frac, hex_col in self.segments:
            if frac <= 0:
                continue
            r, g, b = hex_to_rgb(hex_col)
            slices.append((frac, color_of(r, g, b, 1.0), True))
        if self.other_fraction > 0:
            slices.append((self.other_fraction, color_of(1, 1, 1, 0.42), False))

        widths = []
        for frac, _color, is_server in slices:
            seg_w = width * frac
            # A few MB against several GB is under a pixel and vanishes inside the
            # rounded end. Keep a hairline so each server color still shows.
            if is_server and 0 < seg_w < 3.0:
                seg_w = 3.0
            widths.append(seg_w)

        used = sum(widths)
        if used > width and used > 0:
            scale = width / used
            widths = [seg_w * scale for seg_w in widths]

        x = 0.0
        for seg_w, (_frac, color, _is_server) in zip(widths, slices):
            fill(x, seg_w, color)
            x += seg_w

        snapshot.pop()

class Sparkline(Gtk.DrawingArea):
    """Mini 10-minute memory/CPU trend curve."""
    def __init__(self, color_hex: str = "#6EC7ED", width: int = 48, height: int = 16):
        super().__init__()
        self.color_hex = color_hex
        self.set_content_width(width)
        self.set_content_height(height)
        self.points: List[float] = []
        self.set_draw_func(self._on_draw)

    def set_points(self, points: List[float], color_hex: str = None):
        self.points = points
        if color_hex:
            self.color_hex = color_hex
        self.queue_draw()

    def _on_draw(self, area, cr, width, height):
        if not self.points:
            # Draw subtle flat line in center
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.15)
            cr.set_line_width(1.0)
            cr.move_to(0, height / 2.0)
            cr.line_to(width, height / 2.0)
            cr.stroke()
            return

        min_val = min(self.points)
        max_val = max(self.points)
        val_range = max_val - min_val

        # If flat
        if val_range < 0.001:
            val_range = 1.0
            min_val = min_val - 0.5

        r, g, b = hex_to_rgb(self.color_hex)
        cr.set_source_rgb(r, g, b)
        cr.set_line_width(1.2)

        step_x = width / max(1, len(self.points) - 1)
        pad = 2.0
        avail_h = height - (pad * 2)

        for i, val in enumerate(self.points):
            x = i * step_x
            norm = (val - min_val) / val_range
            y = (height - pad) - (norm * avail_h)
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)

        cr.stroke()

class DetailChart(Gtk.DrawingArea):
    """10-minute full detail history chart (Memory curve or CPU bars)."""
    def __init__(self, is_cpu: bool = False, color_hex: str = "#6EC7ED"):
        super().__init__()
        self.is_cpu = is_cpu
        self.color_hex = color_hex
        self.set_content_height(60)
        self.set_hexpand(True)
        self.samples: List[float] = []
        self.threshold_val: float = 0.0
        self.set_draw_func(self._on_draw)

    def set_data(self, samples: List[float], threshold_val: float = 0.0, color_hex: str = None):
        self.samples = samples
        self.threshold_val = threshold_val
        if color_hex:
            self.color_hex = color_hex
        self.queue_draw()

    def _on_draw(self, area, cr, width, height):
        # Subtle horizontal guide lines at 0%, 50%, 100%
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.05)
        cr.set_line_width(1.0)
        for y_pct in (0.2, 0.5, 0.8):
            cr.move_to(0, height * y_pct)
            cr.line_to(width, height * y_pct)
            cr.stroke()

        if not self.samples:
            return

        max_val = max(self.samples) if self.samples else 1.0
        if self.threshold_val > 0:
            max_val = max(max_val, self.threshold_val * 1.1)
        if self.is_cpu:
            max_val = max(max_val, 100.0)
        if max_val <= 0:
            max_val = 1.0

        pad_top = 4.0
        pad_bot = 4.0
        chart_h = height - pad_top - pad_bot

        # Draw threshold guide if present (amber)
        if self.threshold_val > 0:
            norm_thresh = self.threshold_val / max_val
            thresh_y = (height - pad_bot) - (norm_thresh * chart_h)
            ar, ag, ab = hex_to_rgb(f"#{PALETTE_AMBER[1]:06x}")
            cr.set_source_rgba(ar, ag, ab, 0.4)
            cr.set_dash([4.0, 3.0])
            cr.move_to(0, thresh_y)
            cr.line_to(width, thresh_y)
            cr.stroke()
            cr.set_dash([])  # reset dash

        r, g, b = hex_to_rgb(self.color_hex)

        if self.is_cpu:
            # CPU histogram bars
            bar_count = len(self.samples)
            bar_w = max(2.0, (width / max(1, bar_count)) - 1.5)
            cr.set_source_rgba(r, g, b, 0.8)
            for i, val in enumerate(self.samples):
                norm = min(1.0, val / max_val)
                h = norm * chart_h
                x = i * (width / max(1, bar_count))
                y = (height - pad_bot) - h
                cr.rectangle(x, y, bar_w, h)
                cr.fill()
        else:
            # Memory continuous line + light fill
            step_x = width / max(1, len(self.samples) - 1)
            cr.set_source_rgba(r, g, b, 0.9)
            cr.set_line_width(1.5)

            pts = []
            for i, val in enumerate(self.samples):
                norm = min(1.0, val / max_val)
                x = i * step_x
                y = (height - pad_bot) - (norm * chart_h)
                pts.append((x, y))
                if i == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.stroke()

            # End dot
            if pts:
                last_x, last_y = pts[-1]
                cr.arc(last_x, last_y, 2.5, 0, 6.2831853)
                cr.fill()
