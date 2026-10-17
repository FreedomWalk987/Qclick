# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Q&Click N-sidebar editor.

GPU-drawn editor anchored to a headerless placeholder panel: main wheel
display and editing (drag native Quick Favorites onto slots, clear, reset)
plus simple favorites editing (select, move up/down, remove).  All data comes
from this add-on's own native bridge and persistence; every visual value
derives from the active Blender theme so the surface matches native widgets.
"""

import copy
import colorsys
import json
import math
import time

import blf
import bpy
from bpy.app.handlers import persistent
from .runtime import log, redraw_area, redraw_regions, space_class
import gpu
from bpy.types import Operator, Panel
from bpy.props import BoolProperty, StringProperty
from gpu_extras.batch import batch_for_shader

from . import native_bridge, persistence, runtime
from .i18n import T

SLOT_DIRECTIONS = ("Left", "Right", "Down", "Up", "Up Left", "Up Right", "Down Left", "Down Right")
SLOT_VECTORS = (
    (-1.0, 0.0),
    (1.0, 0.0),
    (0.0, -1.0),
    (0.0, 1.0),
    (-0.72, 0.52),
    (0.72, 0.52),
    (-0.72, -0.52),
    (0.72, -0.52),
)

EDITOR_STATUS_HEIGHT = 20.0
EDITOR_GAP = 3.0
EDITOR_MIN_WIDTH = 170.0
PANEL_ROW_HEIGHT = 20.0
# Text metrics in unscaled pixels; multiply by the theme scale when drawing.
EDITOR_FONT_SIZE = 11
EDITOR_STATUS_FONT_SIZE = 10
EDITOR_TEXT_PADDING = 4.0
NATIVE_REFRESH_TICKS = 8  # housekeeping runs at 0.2s; refresh every ~1.6s

_UNIFORM_SHADER = None
_SMOOTH_SHADER = None
_REGISTERED = False
_SESSIONS = {}
_PANEL_HEARTBEATS = {}
_SELECTED_SOURCE = 0
_SELECTED_SLOT = -1
_STATUS = ""  # empty shows DEFAULT_STATUS in the current language
DEFAULT_STATUS = "Right-click to rename; tick on the right to record as last action"
_READ_ERROR = ""
_MONITOR_TICKS = 0


def _ui_snapshot(context, layer_id=persistence.ROOT, sources="FAVORITES", profile=None):
    """Return (favorite action records, current-mode wheel slots)."""
    global _READ_ERROR
    profile = persistence.load_profile(context) if profile is None else profile
    try:
        if sources == "SHORTCUTS":
            records = persistence.action_sources(context, profile)
        elif sources == "FAVORITES":
            records = native_bridge._native_quick_favorites(context)
        else:
            records = persistence.folder_entries(context, "GROUP" if sources == "GROUPS" else "FOLDER", profile=profile)
        _READ_ERROR = ""
    except Exception as exc:
        records = []
        _READ_ERROR = str(exc)
    return (tuple(records), tuple(persistence.profile_slots(profile, layer_id)))


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def _rgba(value, fallback):
    try:
        components = tuple(float(component) for component in value)
        if len(components) == 3:
            return (*components, fallback[3])
        if len(components) >= 4:
            return components[:4]
    except Exception:
        pass
    return fallback


def _clamp01(value):
    return max(0.0, min(1.0, float(value)))


def _blend(first, second, amount, alpha=None):
    amount = _clamp01(amount)
    result = tuple(first[index] + (second[index] - first[index]) * amount for index in range(4))
    if alpha is not None:
        result = (result[0], result[1], result[2], float(alpha))
    return result


def _shade(color, offset):
    """Apply a Blender-style shadetop/shadedown offset (-100..100) to a color."""
    amount = float(offset) / 255.0
    return (
        _clamp01(color[0] + amount),
        _clamp01(color[1] + amount),
        _clamp01(color[2] + amount),
        color[3],
    )


_FALLBACK_WCOL = {
    "inner": (0.33, 0.33, 0.33, 1.0),
    "inner_sel": (0.28, 0.45, 0.70, 1.0),
    "outline": (0.24, 0.24, 0.24, 1.0),
    "item": (0.30, 0.30, 0.30, 1.0),
    "text": (0.90, 0.90, 0.90, 1.0),
    "text_sel": (1.0, 1.0, 1.0, 1.0),
    "show_shaded": False,
    "shadetop": 0,
    "shadedown": 0,
    "roundness": 0.4,
}


def _native_theme():
    """Collect the widget styles the editor mirrors from the active theme."""
    theme = {
        "wcol": {key: dict(_FALLBACK_WCOL) for key in ("tool", "list", "box", "pie", "scroll", "regular", "option", "tooltip")},
        "emboss": (1.0, 1.0, 1.0, 0.02),
        "scale": 1.0,
        "unit": 20.0,
    }
    try:
        ui = bpy.context.preferences.themes[0].user_interface
        for key, name in (
            ("tool", "wcol_tool"),
            ("list", "wcol_list_item"),
            ("box", "wcol_box"),
            ("pie", "wcol_pie_menu"),
            ("scroll", "wcol_scroll"),
            ("regular", "wcol_regular"),
            ("option", "wcol_option"),
            ("tooltip", "wcol_tooltip"),
        ):
            widget = getattr(ui, name)
            fallback = _FALLBACK_WCOL
            theme["wcol"][key] = {
                "inner": _rgba(widget.inner, fallback["inner"]),
                "inner_sel": _rgba(widget.inner_sel, fallback["inner_sel"]),
                "outline": _rgba(widget.outline, fallback["outline"]),
                "item": _rgba(widget.item, fallback["item"]),
                "text": _rgba(widget.text, fallback["text"]),
                "text_sel": _rgba(widget.text_sel, fallback["text_sel"]),
                "show_shaded": bool(widget.show_shaded),
                "shadetop": int(widget.shadetop),
                "shadedown": int(widget.shadedown),
                "roundness": float(widget.roundness),
            }
        theme["emboss"] = _rgba(ui.widget_emboss, theme["emboss"])
        theme["scale"] = max(0.75, float(bpy.context.preferences.system.ui_scale))
        theme["unit"] = 20.0 * theme["scale"]
    except Exception:
        pass
    return theme


# ---------------------------------------------------------------------------
# GPU primitives
# ---------------------------------------------------------------------------


def _uniform_shader():
    global _UNIFORM_SHADER
    if _UNIFORM_SHADER is None:
        _UNIFORM_SHADER = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _UNIFORM_SHADER


def _smooth_shader():
    global _SMOOTH_SHADER
    if _SMOOTH_SHADER is None:
        _SMOOTH_SHADER = gpu.shader.from_builtin("SMOOTH_COLOR")
    return _SMOOTH_SHADER


def _rect_from_center(x, y, width, height):
    return (x - width * 0.5, y - height * 0.5, x + width * 0.5, y + height * 0.5)


def _rect_center(rect):
    return ((rect[0] + rect[2]) * 0.5, (rect[1] + rect[3]) * 0.5)


def _rect_size(rect):
    return (rect[2] - rect[0], rect[3] - rect[1])


def _point_in_rect(point, rect):
    return bool(rect and rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3])


def _rounded_points(rect, radius, corner_segments=6):
    left, bottom, right, top = (float(value) for value in rect)
    radius = max(0.0, min(float(radius), (right - left) * 0.5, (top - bottom) * 0.5))
    if radius <= 0.01:
        return ((left, bottom), (right, bottom), (right, top), (left, top))
    corners = (
        (right - radius, bottom + radius, -math.pi * 0.5, 0.0),
        (right - radius, top - radius, 0.0, math.pi * 0.5),
        (left + radius, top - radius, math.pi * 0.5, math.pi),
        (left + radius, bottom + radius, math.pi, math.pi * 1.5),
    )
    points = []
    for cx, cy, start, end in corners:
        for step in range(corner_segments + 1):
            angle = start + (end - start) * (step / corner_segments)
            points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return tuple(points)


def _draw_gradient_fill(rect, points, top_color, bottom_color):
    height = max(0.001, rect[3] - rect[1])

    def vertex_color(point):
        amount = _clamp01((point[1] - rect[1]) / height)
        return _blend(bottom_color, top_color, amount)

    center = _rect_center(rect)
    center_color = vertex_color(center)
    coords = []
    colors = []
    for index, point in enumerate(points):
        following = points[(index + 1) % len(points)]
        coords.extend((center, point, following))
        colors.extend((center_color, vertex_color(point), vertex_color(following)))
    shader = _smooth_shader()
    shader.bind()
    batch_for_shader(shader, "TRIS", {"pos": coords, "color": colors}).draw(shader)


def _draw_flat_fill(rect, points, color):
    center = _rect_center(rect)
    coords = []
    for index, point in enumerate(points):
        coords.extend((center, point, points[(index + 1) % len(points)]))
    shader = _uniform_shader()
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRIS", {"pos": coords}).draw(shader)


def _draw_outline(points, color, width=1.0):
    shader = _uniform_shader()
    try:
        gpu.state.line_width_set(width)
    except Exception:
        pass
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "LINE_LOOP", {"pos": points}).draw(shader)
    try:
        gpu.state.line_width_set(1.0)
    except Exception:
        pass


def _draw_line(start, end, color, width=1.0):
    shader = _uniform_shader()
    try:
        gpu.state.line_width_set(width)
    except Exception:
        pass
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "LINES", {"pos": (start, end)}).draw(shader)
    try:
        gpu.state.line_width_set(1.0)
    except Exception:
        pass


_POLYLINE_SHADER = None


def _polyline_shader():
    """Blender's anti-aliased wide-line shader; None if this build lacks it."""
    global _POLYLINE_SHADER
    if _POLYLINE_SHADER is None:
        try:
            _POLYLINE_SHADER = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
        except Exception:
            _POLYLINE_SHADER = False
    return _POLYLINE_SHADER or None


def _draw_smooth_paths(paths, color, width):
    """Draw open polylines with smooth edges and even stroke width.

The plain UNIFORM_COLOR line used elsewhere is aliased and its width is left
to the driver, which turns small curves into jagged pixel stairs.
"""
    shader = _polyline_shader()
    if shader is None:
        for path in paths:
            for first, second in zip(path, path[1:]):
                _draw_line(first, second, color, width)
        return
    viewport = gpu.state.viewport_get()
    shader.bind()
    shader.uniform_float("viewportSize", (float(viewport[2]), float(viewport[3])))
    shader.uniform_float("lineWidth", float(width))
    shader.uniform_float("color", color)
    for path in paths:
        batch_for_shader(shader, "LINE_STRIP", {"pos": path}).draw(shader)


# Icons are drawn the way Blender draws its own: as small pre-rendered,
# anti-aliased bitmaps placed on whole pixels, not as live GPU lines (which
# come out jagged or blobby at this size).  Geometry is a 16-unit grid, y up,
# matching Blender's 16 px icon size; strokes are in grid units.
ICON_SIZE = 16.0
ICON_STROKE = 1.15


def _icon_reset_geometry():
    """Return arrow, like Blender's loop-back icon: an arrowhead pointing left
at the top, a bar that turns back down around a half circle, and a short tail
along the bottom."""
    top, bottom, turn_x = 11.5, 4.5, 9.0
    radius = (top - bottom) * 0.5
    centre_y = (top + bottom) * 0.5
    curve = [(turn_x + radius * math.cos(math.radians(angle)), centre_y + radius * math.sin(math.radians(angle)))
             for angle in range(90, -91, -10)]
    body = ((3.5, top), *curve, (6.5, bottom))
    head = ((5.8, top + 2.3), (3.5, top), (5.8, top - 2.3))
    return (body, head)


ICON_GEOMETRY = {
    "X": (((4.5, 4.5), (11.5, 11.5)), ((4.5, 11.5), (11.5, 4.5))),
    "RESET": _icon_reset_geometry(),
}
# Width actually inked by each icon, in grid units, for laying out icon+label.
ICON_INK_WIDTH = {"X": 7.0 + ICON_STROKE, "RESET": 9.0 + ICON_STROKE}


def _segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length <= 0.0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    qx, qy = ax + dx * t - px, ay + dy * t - py
    return math.sqrt(qx * qx + qy * qy)


def _rasterize_icon(paths, size, stroke):
    """Coverage (0..1) per pixel, bottom row first, for strokes along paths."""
    unit = size / ICON_SIZE
    half = stroke * unit * 0.5
    segments = [(a[0] * unit, a[1] * unit, b[0] * unit, b[1] * unit)
                for path in paths for a, b in zip(path, path[1:])]
    coverage = []
    for row in range(size):
        cy = row + 0.5
        for column in range(size):
            cx = column + 0.5
            distance = min(_segment_distance(cx, cy, *segment) for segment in segments)
            coverage.append(max(0.0, min(1.0, half + 0.5 - distance)))
    return coverage


_ICON_BATCHES = {}


def _icon_batch(name, size, color, bg_top=None, bg_bottom=None):
    """One small quad per inked pixel, precomposed onto the button background
when given so anti-aliased edge pixels stay opaque (alpha=1.0) and never punch
translucent holes into the region framebuffer."""
    bg_key = (
        tuple(round(float(c), 3) for c in bg_top) if bg_top is not None else None,
        tuple(round(float(c), 3) for c in bg_bottom) if bg_bottom is not None else None,
    )
    key = (name, size, tuple(round(float(c), 3) for c in color), bg_key)
    batch = _ICON_BATCHES.get(key)
    if batch is None:
        if len(_ICON_BATCHES) > 48:
            _ICON_BATCHES.clear()
        coverage = _rasterize_icon(ICON_GEOMETRY[name], size, ICON_STROKE)
        r, g, b, a = (float(c) for c in color)
        if bg_top is not None and bg_bottom is None:
            bg_bottom = bg_top
        coords, colors = [], []
        for index, value in enumerate(coverage):
            if value <= 0.004:
                continue
            x, y = index % size, index // size
            coords.extend(((x, y), (x + 1, y), (x + 1, y + 1), (x, y), (x + 1, y + 1), (x, y + 1)))
            if bg_top is not None:
                row_bg = _blend(bg_bottom, bg_top, (y + 0.5) / max(1, size), alpha=1.0)
                pixel_color = _blend(row_bg, (r, g, b, 1.0), a * value, alpha=1.0)
            else:
                pixel_color = (r, g, b, a * value)
            colors.extend([pixel_color] * 6)
        batch = batch_for_shader(_smooth_shader(), "TRIS", {"pos": coords, "color": colors})
        _ICON_BATCHES[key] = batch
    return batch


def _draw_icon(name, center, color, scale, bg_top=None, bg_bottom=None):
    size = max(8, int(round(ICON_SIZE * scale)))
    # Whole-pixel placement keeps one bitmap pixel per screen pixel, as native icons do.
    x0 = math.floor(center[0] - size * 0.5 + 0.5)
    y0 = math.floor(center[1] - size * 0.5 + 0.5)
    try:
        gpu.state.blend_set("ALPHA")
        batch = _icon_batch(name, size, color, bg_top, bg_bottom)
        shader = _smooth_shader()
        gpu.matrix.push()
        try:
            gpu.matrix.translate((x0, y0))
            shader.bind()
            batch.draw(shader)
        finally:
            gpu.matrix.pop()
    except Exception as exc:
        log.debug("icon bitmap unavailable, drawing strokes: %s", exc)
        unit = size / ICON_SIZE
        paths = [[(x0 + px * unit, y0 + py * unit) for px, py in path] for path in ICON_GEOMETRY[name]]
        _draw_smooth_paths(paths, color, max(1.0, ICON_STROKE * unit))


def _draw_icon_x(center, color, scale, bg_top=None, bg_bottom=None):
    _draw_icon("X", center, color, scale, bg_top, bg_bottom)


def _draw_icon_reset(center, color, scale, bg_top=None, bg_bottom=None):
    _draw_icon("RESET", center, color, scale, bg_top, bg_bottom)


def _widget_radius(wcol, theme, rect):
    width, height = _rect_size(rect)
    radius = wcol["roundness"] * theme["unit"] * 0.5
    return max(0.0, min(radius, width * 0.5, height * 0.5))


def _hsl_tint(color, saturation, lightness):
    hue, light, sat = colorsys.rgb_to_hls(*color[:3])
    return (*colorsys.hls_to_rgb(hue, _clamp01(light * lightness), _clamp01(sat * saturation)), color[3])


def _widget_style(wcol, state="normal", role="regular"):
    """Resolve theme roles using Blender's regular/list/pie state distinctions.

    Reference: Blender 4.5 interface_widgets.cc, widget_state,
    widget_active_color, widget_list_itembut and widget_state_pie_menu_item.
    Return a copy: drawing must never mutate the active theme or source palette.
    """
    style = dict(wcol)
    selected = state in {"pressed", "selected", "selected_hover"}
    hovered = state in {"hover", "selected_hover"}
    if selected:
        style["inner"], style["text"] = wcol["inner_sel"], wcol["text_sel"]
        if role != "pie":
            style["shadetop"], style["shadedown"] = wcol["shadedown"], wcol["shadetop"]
    elif hovered:
        if role == "pie":
            style["inner"], style["text"] = wcol["item"], wcol["text_sel"]
        else:
            weights = (0.2126, 0.7152, 0.0722)
            dark = sum(c * w for c, w in zip(wcol["text"], weights)) > sum(c * w for c, w in zip(wcol["inner"], weights))
            style["inner"] = _hsl_tint(wcol["inner"], 1.15, 1.2 if dark else 1.1)
            style["outline"] = _hsl_tint(wcol["outline"], 1.15, 1.15)
            style["text"] = _hsl_tint(wcol["text"], 1.15, 1.25 if dark else 0.8)
    if role == "list" and hovered:
        style["inner"] = _blend(style["inner"], style["text"], 0.2, alpha=1.0 if selected else 20.0 / 255.0)
    return style


def _draw_widget(rect, wcol, theme, *, state="normal", role="regular", fill=None, border=None, border_width=1.0, emboss=False, alpha=1.0):
    """Draw one native-style widget body: shaded fill, outline, emboss line."""
    wcol = _widget_style(wcol, state, role)
    if fill is None:
        fill = wcol["inner"]
    if border is None:
        border = wcol["outline"]
    if alpha < 1.0:
        fill = (fill[0], fill[1], fill[2], fill[3] * alpha)
        border = (border[0], border[1], border[2], border[3] * alpha)
    radius = _widget_radius(wcol, theme, rect)
    points = _rounded_points(rect, radius)
    if fill[3] > 0.0:
        if wcol["show_shaded"]:
            _draw_gradient_fill(rect, points, _shade(fill, wcol["shadetop"]), _shade(fill, wcol["shadedown"]))
        else:
            _draw_flat_fill(rect, points, fill)
    if border[3] > 0.0:
        _draw_outline(points, border, border_width)
    if emboss and theme["emboss"][3] > 0.0:
        color = theme["emboss"]
        if alpha < 1.0:
            color = (color[0], color[1], color[2], color[3] * alpha)
        _draw_line(
            (rect[0] + radius, rect[1] - 1.0),
            (rect[2] - radius, rect[1] - 1.0),
            color,
        )


def _fit_text_px(font_id, text, max_width):
    text = str(text or "")
    if max_width <= 4.0:
        return ""
    if blf.dimensions(font_id, text)[0] <= max_width:
        return text
    while text and blf.dimensions(font_id, text + "…")[0] > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


# blf reports a string's box as ascent plus the font's descent, but CJK labels
# and most of ours put no ink below the baseline.  Centring on that box drops
# the visible glyphs by half the descent, so bias the baseline back up.
TEXT_DESCENT_RATIO = 0.2


def _draw_text(text, rect, size, color, *, align="CENTER", padding=4.0):
    font_id = 0
    blf.size(font_id, max(8, int(round(size))))
    available = rect[2] - rect[0] - padding * 2.0
    text = _fit_text_px(font_id, text, available)
    if not text:
        return
    width, height = blf.dimensions(font_id, text)
    if align == "LEFT":
        x = rect[0] + padding
    elif align == "RIGHT":
        x = rect[2] - padding - width
    else:
        x = (rect[0] + rect[2] - width) * 0.5
    y = (rect[1] + rect[3] - height) * 0.5 + size * TEXT_DESCENT_RATIO
    blf.position(font_id, x, y, 0)
    blf.color(font_id, *color)
    blf.enable(font_id, blf.CLIPPING)
    blf.clipping(font_id, rect[0], rect[1], rect[2], rect[3])
    try:
        blf.draw(font_id, text)
    finally:
        blf.disable(font_id, blf.CLIPPING)
        # BLF alters GPU blend state when flushing glyphs; restore alpha blending.
        try:
            gpu.state.blend_set("ALPHA")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Delayed, draw-only tooltips shared with the runtime wheel
# ---------------------------------------------------------------------------


class _HoverTip:
    def __init__(self):
        self.reset()

    def reset(self):
        self.key = None
        self.point = (0, 0)
        self.started = 0.0

    def ready(self, key, point, now=None):
        now = time.monotonic() if now is None else now
        if key is None:
            self.reset()
            return False
        if key != self.key or math.dist(point, self.point) > 5:
            self.key, self.point, self.started = key, point, now
            return False
        return now - self.started >= 0.55


def _tip_content(entry, profile):
    if not entry:
        return ()
    lines = [persistence.display_label(entry, profile)]
    kind = entry.get("kind", "ACTION")
    if kind in {"FOLDER", "GROUP"}:
        lines.append(T("Sub-wheel") if kind == "FOLDER" else T("Four-item group"))
    elif entry.get("action_type", "OPERATOR") == "OPERATOR":
        lines.append(str(entry.get("operator", "")))
        if entry.get("properties"):
            value = json.dumps(entry["properties"], ensure_ascii=False, sort_keys=True)
            lines.append(T("Parameters: {value}", value=value[:280] + ("…" if len(value) > 280 else "")))
    elif entry.get("action_type") == "MENU":
        lines.append(str(entry.get("menu", "")))
    else:
        lines.append(str(entry.get("data_path", "")) + "." + str(entry.get("property", "")))
    return tuple(lines)


def _wrap_tip(text, width, measure):
    lines, current = [], ""
    for char in str(text):
        if char == "\n" or (current and measure(current + char) > width):
            lines.append(current)
            current = "" if char == "\n" else char
        else:
            current += char
    lines.append(current)
    return lines


def _tip_rect(point, size, bounds):
    left, bottom, right, top = bounds
    width, height = min(size[0], right - left), min(size[1], top - bottom)
    x = max(left, min(point[0] + 12, right - width))
    y = point[1] - 18 - height
    if y < bottom:
        y = point[1] + 18
    y = max(bottom, min(y, top - height))
    return (x, y, x + width, y + height)


def _draw_tip(content, point, bounds, theme):
    scale = theme["scale"]
    pad, row = 8 * scale, 16 * scale
    available_width, available_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if not content or available_width < 60 or available_height < row + 2 * pad:
        return None
    width = min(420 * scale, available_width)
    blf.size(0, max(8, int(round(EDITOR_FONT_SIZE * scale))))
    lines = [line for text in content for line in _wrap_tip(text, width - 2 * pad, lambda value: blf.dimensions(0, value)[0])]
    limit = max(1, int((available_height - 2 * pad) // row))
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = "…"
    width = min(width, max(blf.dimensions(0, line)[0] for line in lines) + 2 * pad)
    rect = _tip_rect(point, (width, len(lines) * row + 2 * pad), bounds)
    palette = theme["wcol"]["tooltip"]
    _draw_widget(rect, palette, theme)
    for index, line in enumerate(lines):
        top = rect[3] - pad - index * row
        _draw_text(line, (rect[0] + pad, top - row, rect[2] - pad, top), EDITOR_FONT_SIZE * scale, palette["text"], align="LEFT", padding=0)
    return rect


# Layout


def _panel_anchor(region):
    """Region-space position of the placeholder panel's top-left corner."""
    try:
        x, y = region.view2d.view_to_region(0.0, 0.0, clip=False)
        return (float(x), float(y))
    except Exception:
        return (0.0, float(region.height))


def _editor_theme(region):
    theme = _native_theme()
    first = region.view2d.view_to_region(0.0, 0.0, clip=False)
    second = region.view2d.view_to_region(1000.0, 0.0, clip=False)
    zoom = max(0.1, abs(second[0] - first[0]) / 1000.0)
    theme["scale"] *= zoom
    theme["unit"] *= zoom
    # Native sidebar panels use space.panelcolors, not the box widget color.
    panel = dict(theme["wcol"]["box"])
    background = panel["inner"]
    try:
        background = _rgba(
            bpy.context.preferences.themes[0].view_3d.space.panelcolors.back,
            background,
        )
    except (AttributeError, IndexError, TypeError):
        pass
    panel["inner"] = (*background[:3], 1.0)
    panel["show_shaded"] = False
    theme["panel"] = panel
    return theme


# Insets of the drawn boxes, in unscaled pixels.  The panel's view origin sits
# slightly left of where Blender actually starts a real sidebar panel, so the
# whole block is nudged right to line up with the native "Settings Import and Export" panel
# below it.  Both edges shift together: the width itself already matched.
EDITOR_LEFT_INSET = 6.0
EDITOR_RIGHT_INSET = 28.0


def _editor_horizontal_bounds(region, anchor, scale):
    """Reserve native category width independently of sidebar content zoom."""
    try:
        ui_scale = max(0.75, float(bpy.context.preferences.system.ui_scale))
    except (AttributeError, TypeError, ValueError):
        ui_scale = 1.0
    return (anchor[0] + 2.0 * scale + EDITOR_LEFT_INSET * ui_scale,
            float(region.width) - EDITOR_RIGHT_INSET * ui_scale)


# Height of the drawn panel header, in unscaled pixels, matching a native
# sidebar panel header.
EDITOR_HEADER_HEIGHT = 20.0

# Collapsed state per 3D viewport area, mirroring how Blender remembers a
# panel's open/closed state per area rather than globally.
_EDITOR_COLLAPSED = {}
_SOURCE_SECTION_TITLES = {
    "FAVORITES": "Favorites",
    "GROUPS": "Groups",
    "FOLDERS": "Sub-wheels",
    "SHORTCUTS": "Library",
}


def editor_collapsed(area_pointer):
    return dict(_EDITOR_COLLAPSED.get(int(area_pointer), {}))


def toggle_editor_collapsed(area_pointer, section):
    state = _EDITOR_COLLAPSED.setdefault(int(area_pointer), {})
    state[section] = not state.get(section, False)


def _editor_layout(region, anchor, favorite_count, scroll, theme, group_mode=False, custom_sources=False,
                   collapsed=None):
    """Two stacked sub-panels -- the wheel and the source list -- each with its
own native-style header that collapses independently.

`collapsed` is a mapping of section name to bool; missing sections are open.
"""
    collapsed = collapsed or {}
    wheel_closed = bool(collapsed.get("wheel"))
    sources_closed = bool(collapsed.get("sources"))

    scale = theme["scale"]
    unit = theme["unit"]
    left, right = _editor_horizontal_bounds(region, anchor, scale)
    top = anchor[1] - 2.0 * scale
    usable_width = right - left
    if usable_width < EDITOR_MIN_WIDTH * scale:
        notice = (left, top - 40.0 * scale, max(right, left + 40.0 * scale), top - 6.0 * scale)
        return {"too_small": True, "notice": notice, "scale": scale}

    header_height = EDITOR_HEADER_HEIGHT * scale
    status_height = EDITOR_STATUS_HEIGHT * scale
    gap = EDITOR_GAP * scale
    pad = 4.0 * scale
    control_gap = 3.0 * scale
    footer_height = unit + control_gap + pad
    outer_top = top

    wheel_header = (left, outer_top - header_height, right, outer_top)
    wheel_height = 0.0 if wheel_closed else 240.0 * scale
    wheel = (left, wheel_header[1] - wheel_height, right, wheel_header[1])

    sources_top = wheel[1] - gap
    sources_header = (left, sources_top - header_height, right, sources_top)
    sources_height = 0.0 if sources_closed else 210.0 * scale
    favorites = (left, sources_header[1] - sources_height, right, sources_header[1])

    status = (left, favorites[1] - gap - status_height, right, favorites[1] - gap)
    editor = (left, status[1], right, outer_top)

    tab_height = 20.0 * scale
    tab_gap = 2.0 * scale
    source_heading_height = pad + tab_height * 2 + tab_gap + control_gap

    wheel_content = (wheel[0] + pad, wheel[1] + footer_height, wheel[2] - pad, wheel[3] - pad)
    favorite_frame = (favorites[0] + pad, favorites[1] + footer_height, favorites[2] - pad,
                      favorites[3] - source_heading_height)
    scrollbar_width = 5.0 * scale
    favorite_content = (
        favorite_frame[0] + 2.0 * scale,
        favorite_frame[1] + 3.0 * scale,
        favorite_frame[2],
        favorite_frame[3] - 3.0 * scale,
    )

    buttons = {
        "collapse_wheel": wheel_header,
        "collapse_sources": sources_header,
    }

    slot_rects = ()
    center_x, center_y = _rect_center(wheel_content)
    if not wheel_closed:
        content_width, content_height = _rect_size(wheel_content)
        slot_width = min(96.0 * scale, content_width * 0.28)
        slot_height = 22.0 * scale
        radius_x = min(126.0 * scale, (content_width - slot_width - 4.0 * scale) * 0.5)
        radius_y = min(118.0 * scale, (content_height - slot_height - 10.0 * scale) * 0.5)
        slot_rects = tuple(
            _rect_from_center(center_x + dx * radius_x, center_y + dy * radius_y, slot_width, slot_height)
            for dx, dy in SLOT_VECTORS
        )
        if group_mode:
            slot_rects = tuple(_rect_from_center(center_x, center_y + (1.5 - index) * 32 * scale,
                                                 content_width - 12 * scale, slot_height) for index in range(4))

        button_gap = control_gap
        # Sit the reset/clear row 2 px lower than the panel padding.
        footer_drop = 2.0 * scale
        left_footer = (wheel[0] + pad, wheel[1] + pad - footer_drop, wheel[2] - pad,
                       wheel[1] + pad - footer_drop + unit)
        left_button_width = (left_footer[2] - left_footer[0] - button_gap) * 0.5
        # Clear keeps the right half; reset is a square icon button just left of it.
        buttons["clear"] = (left_footer[0] + left_button_width + button_gap, left_footer[1], left_footer[2], left_footer[3])
        buttons["reset"] = (buttons["clear"][0] - button_gap - unit, left_footer[1],
                            buttons["clear"][0] - button_gap, left_footer[3])
    # The return control lives in the wheel header, the way a native panel puts
    # small controls beside its title.
    buttons["back"] = (wheel_header[2] - pad - 40 * scale, wheel_header[1] + 2 * scale,
                       wheel_header[2] - pad, wheel_header[3] - 2 * scale)

    row_height = unit
    source_rects = []
    visible_rows = 0
    max_scroll = 0
    if sources_closed:
        scroll = 0
    else:
        visible_rows = max(1, int((favorite_content[3] - favorite_content[1]) // row_height))
        max_scroll = max(0, int(favorite_count) - visible_rows)
        scroll = max(0, min(int(scroll), max_scroll))
        for visual_index in range(visible_rows):
            source_index = scroll + visual_index
            if source_index >= favorite_count:
                break
            row_top = favorite_content[3] - visual_index * row_height
            source_rects.append((source_index, (favorite_content[0], row_top - row_height, favorite_content[2], row_top)))

        tab_width = (favorites[2] - favorites[0] - pad * 2 - tab_gap) / 2
        for index, name in enumerate(("show_favorites", "show_groups", "show_folders", "show_shortcuts")):
            x = favorites[0] + pad + (index % 2) * (tab_width + tab_gap)
            tab_top = favorites[3] - pad - (index // 2) * (tab_height + tab_gap)
            buttons[name] = (x, tab_top - tab_height, x + tab_width, tab_top)

        right_footer = (favorites[0] + pad, favorites[1] + pad, favorites[2] - pad, favorites[1] + pad + unit)
        footer_buttons = ("up", "manual", "down", "remove") if custom_sources else ("up", "down", "remove")
        button_gap = control_gap
        right_button_width = (right_footer[2] - right_footer[0] - button_gap * (len(footer_buttons) - 1)) / len(footer_buttons)
        for index, button_id in enumerate(footer_buttons):
            x0 = right_footer[0] + index * (right_button_width + button_gap)
            buttons[button_id] = (x0, right_footer[1], x0 + right_button_width, right_footer[3])

    return {
        "too_small": False,
        "scale": scale,
        "editor": editor,
        "status": status,
        "wheel_header": wheel_header,
        "sources_header": sources_header,
        "wheel_collapsed": wheel_closed,
        "sources_collapsed": sources_closed,
        "wheel": wheel,
        "favorites": favorites,
        "favorite_frame": favorite_frame,
        "favorite_content": favorite_content,
        "scrollbar_width": scrollbar_width,
        "slot_rects": slot_rects,
        "source_rects": tuple(source_rects),
        "buttons": buttons,
        "visible_rows": visible_rows,
        "scroll": scroll,
        "max_scroll": max_scroll,
    }


def _repeat_checkbox_key(record):
    """Containers and menus are navigation, not repeatable actions."""
    try:
        return persistence.repeat_key(record)
    except (ValueError, TypeError, AttributeError):
        return None


def _source_row_parts(rect, scale, checkbox=True):
    """Shared label, hit-target and glyph geometry; never widen the sidebar."""
    # Extend only the row background; retain the original content/checkbox lane.
    rect = (rect[0], rect[1], rect[2] - 9.0 * scale, rect[3])
    label = (rect[0] + 4 * scale, rect[1], rect[2] - 2 * scale, rect[3])
    if not checkbox:
        return label, None, None
    hit = (rect[2] - 20 * scale, rect[1], rect[2], rect[3])
    glyph = _rect_from_center(*_rect_center(hit), 14 * scale, 14 * scale)
    return (label[0], label[1], hit[0] - 2 * scale, label[3]), hit, glyph


def _tag_view3d_redraw():
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        return
    for window in windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type in persistence.SUPPORTED_AREAS:
                redraw_regions(area)


def _session_key(window, area, region):
    return (int(window.as_pointer()), int(area.as_pointer()), int(region.as_pointer()))


def _panel_is_visible(area_pointer, region_pointer, region=None):
    # Callers must pass a region they just re-resolved from live window data;
    # a stored one may be freed memory and reading it crashes Blender.
    if region is not None:
        try:
            if region.active_panel_category != _EditorPanelBase.bl_category:
                return False
        except ReferenceError:
            return False
        except Exception:
            pass
    heartbeat = _PANEL_HEARTBEATS.get(int(area_pointer))
    return bool(
        heartbeat
        and heartbeat["region"] == int(region_pointer)
        and time.monotonic() - heartbeat["seen"] <= 0.65
    )


# ---------------------------------------------------------------------------
# Modal session
# ---------------------------------------------------------------------------


class QCLICK2_OT_folder_name(Operator):
    bl_idname = "qclick2.folder_name"
    bl_label = "Name"
    bl_options = {"INTERNAL"}

    operation: StringProperty(default="CREATE", options={"HIDDEN", "SKIP_SAVE"})
    folder_id: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    kind: StringProperty(default="FOLDER", options={"HIDDEN", "SKIP_SAVE"})
    folder_name: StringProperty(name="Name", default="Sub-wheel", maxlen=64)
    _profile = ""

    def invoke(self, context, event):
        self._profile = persistence._profile_key(context)
        return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_SMALL, title=T("Name"))

    def draw(self, context):
        self.layout.prop(self, "folder_name", text=T("Name"))

    def execute(self, context):
        global _SELECTED_SOURCE, _STATUS
        if self._profile and self._profile != persistence._profile_key(context):
            self.report({"WARNING"}, T("The mode changed; the operation was cancelled"))
            return {"CANCELLED"}
        if self.kind not in persistence.LIBRARIES:
            return {"CANCELLED"}
        if self.operation == "CREATE":
            ok, message, folder_id = persistence.create_folder(context, self.folder_name, self.kind)
        else:
            folder_id = self.folder_id
            ok, message = persistence.rename_folder(context, folder_id, self.folder_name, self.kind)
        if not ok:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        _STATUS = T("Added; press the pencil on its row to edit it, or drag it into a slot") if self.operation == "CREATE" else T("Name updated")
        records = persistence.folder_entries(context, self.kind)
        _SELECTED_SOURCE = next((index for index, entry in enumerate(records) if entry["layer_id"] == folder_id), 0)
        _tag_view3d_redraw()
        return {"FINISHED"}


RESET_CONFIRM_WORD = "reset"


class QCLICK2_OT_reset_wheel(Operator):
    bl_idname = "qclick2.reset_wheel"
    bl_label = "Reset Wheel"
    bl_options = {"INTERNAL"}

    layer_id: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    layer_title: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    confirm_text: StringProperty(name="Confirm", default="", options={"SKIP_SAVE"}, maxlen=16)
    _profile = ""

    def invoke(self, context, event):
        self._profile = persistence._profile_key(context)
        self.confirm_text = ""
        return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_MEDIUM,
                                                         title=T("Reset Wheel"), confirm_text=T("Reset"))

    def draw(self, context):
        layout = self.layout
        layout.label(text=T("This clears every slot of \"{layer}\" and cannot be undone.", layer=self.layer_title), icon="ERROR")
        layout.label(text=T("Type {word}, then press Reset:", word=RESET_CONFIRM_WORD))
        layout.prop(self, "confirm_text", text="")

    def execute(self, context):
        global _STATUS
        if self.confirm_text.strip().lower() != RESET_CONFIRM_WORD:
            _STATUS = T("{word} was not typed; the wheel is unchanged", word=RESET_CONFIRM_WORD)
            self.report({"WARNING"}, _STATUS)
            _tag_view3d_redraw()
            return {"CANCELLED"}
        if self._profile and self._profile != persistence._profile_key(context):
            self.report({"WARNING"}, T("The mode changed; reset was cancelled"))
            return {"CANCELLED"}
        changed, message = persistence.reset_slots(context, layer_id=self.layer_id or persistence.ROOT)
        _STATUS = T("Cleared the current wheel") if changed else message
        _tag_view3d_redraw()
        return {"FINISHED"} if changed else {"CANCELLED"}


class _EditorSession:
    """Per-sidebar editor state and drawing; lives as long as its sidebar.

Input arrives through QCLICK2_OT_editor_session, a modal operator attached
only while the pointer is over this sidebar.  Blender skips autosave whenever
any modal operator is running, so a permanently attached editor modal meant
autosave never happened while the add-on was enabled.  State lives here, not
on the operator, so attaching and detaching never resets scroll, layer or
selection.
"""

    _key = None
    _attached = None
    _finished = False
    _window = None
    _area = None
    _region = None
    _draw_handle = None
    _draw_space = None
    _layout = None
    _mouse = (-1000.0, -1000.0)
    _press_mouse = (0.0, 0.0)
    _pending_source = -1
    _drag_source = -1
    _source_slot = -1
    _drag_record = None
    _drag_profile = ""
    _drag_layer = persistence.ROOT
    _layer_path = ()
    _edit_profile = ""
    _source_kind = "FAVORITES"
    _group_mode = False
    _confirm_folder_id = ""
    _drag_slots = ()
    _hover_source = -1
    _hover_slot = -1
    _hover_button = ""
    _pressed_button = ""
    _pressed_check = None
    _repeat_exclusions = frozenset()
    _layer_title = "Main Wheel"
    _scroll = 0
    _records = ()
    _slots = ()
    _confirm_remove_until = 0.0
    _tooltip = None
    _tooltip_suspended = True

    def start(self, window, area, region):
        key = _session_key(window, area, region)
        self._key = key
        self._finished = False
        self._tooltip = _HoverTip()
        self._tooltip_suspended = True
        self._window = window
        self._area = area
        self._region = region
        self._scroll = 0
        self._layer_path = []
        with bpy.context.temp_override(window=window, area=area, region=region):
            self._edit_profile = persistence._profile_key(bpy.context)
        self._hover_button = ""
        self._pressed_button = ""
        self._pressed_check = None
        self._confirm_remove_until = 0.0
        self._draw_space = space_class(area.type)
        self._draw_handle = self._draw_space.draw_handler_add(self._draw, (), "UI", "POST_PIXEL")
        _SESSIONS[key] = self
        redraw_regions(self._area, ("UI",))

    def report(self, kind, message):
        operator = self._attached
        if operator is not None:
            try:
                operator.report(kind, message)
            except Exception:
                pass

    def idle(self):
        """No gesture in flight: safe to stop receiving input."""
        return (self._pending_source < 0 and self._drag_source < 0
                and self._pressed_check is None and not self._pressed_button)

    def detach(self):
        self._attached = None
        self._hover_source = -1
        self._hover_button = ""
        self._hover_slot = -1
        if self._tooltip is not None:
            self._tooltip.reset()
        self._tooltip_suspended = True

    def _layer_id(self):
        return self._layer_path[-1] if self._layer_path else persistence.ROOT

    def _sync_layer(self, context):
        global _SELECTED_SLOT
        key = persistence._profile_key(context)
        profile = persistence.load_profile(context)
        if key != self._edit_profile:
            self._layer_path = []
            self._edit_profile = key
            _SELECTED_SLOT = -1
            self._cancel_drag(message=False)
            self._confirm_remove_until = 0
        for index, layer_id in enumerate(self._layer_path):
            if layer_id not in profile["folders"] and layer_id not in profile["groups"]:
                self._layer_path = self._layer_path[:index]
                _SELECTED_SLOT = -1
                self._cancel_drag(message=False)
                break
        return profile

    def _slot_name(self, index):
        return T("Item {number}", number=index + 1) if self._group_mode else T(SLOT_DIRECTIONS[index])

    def _local_mouse(self, event):
        return (float(event.mouse_x - self._region.x), float(event.mouse_y - self._region.y))

    def _source_at(self, point):
        if not self._inside_content(point) or not self._layout or self._layout.get("too_small"):
            return -1
        for source_index, rect in self._layout.get("source_rects", ()):
            if _point_in_rect(point, rect):
                return int(source_index)
        return -1

    def _checkbox_at(self, point):
        index = self._source_at(point)
        if index < 0 or index >= len(self._records) or not _repeat_checkbox_key(self._records[index]):
            return -1
        rect = dict(self._layout["source_rects"])[index]
        _label, hit, _glyph = _source_row_parts(rect, self._layout["scale"])
        return index if _point_in_rect(point, hit) else -1

    def _commit_checkbox(self, context, press):
        """Reject changed rows/profiles/policy instead of toggling a new target."""
        if (press["cancelled"] or self._checkbox_at(self._mouse) != press["index"]
                or self._source_kind != press["sources"]
                or persistence._profile_key(context) != press["profile"]
                or self._scroll != press["scroll"]):
            return False, T("The tick change was cancelled")
        profile = persistence.load_profile(context)
        records, _slots = _ui_snapshot(context, self._layer_id(), self._source_kind, profile)
        index = press["index"]
        if (index >= len(records) or _repeat_checkbox_key(records[index]) != press["key"]
                or (press["key"] not in profile["repeat_exclusions"]) != press["allowed"]):
            return False, T("The source changed; tick it again")
        return persistence.set_repeat_allowed(context, press["action"], not press["allowed"])

    def _slot_at(self, point):
        if not self._inside_content(point) or not self._layout or self._layout.get("too_small"):
            return -1
        for slot_index, rect in enumerate(self._layout.get("slot_rects", ())):
            if _point_in_rect(point, rect):
                return slot_index
        return -1

    def _group_edit_buttons(self):
        if not self._layout or self._layout.get("too_small"):
            return {}
        scale = self._layout["scale"]
        result = {}
        if self._source_kind not in {"GROUPS", "FOLDERS"}:
            return result
        for index, rect in self._layout["source_rects"]:
            entry = self._records[index] if index < len(self._records) else None
            if isinstance(entry, dict) and entry.get("kind") in {"GROUP", "FOLDER"}:
                _label, _hit, glyph = _source_row_parts(rect, scale, True)
                result["edit_group_" + entry["layer_id"]] = glyph
        return result

    def _button_at(self, point):
        if not self._inside_content(point) or not self._layout or self._layout.get("too_small"):
            return ""
        for button_id, rect in self._group_edit_buttons().items():
            if _point_in_rect(point, rect):
                return button_id
        buttons = self._layout.get("buttons", {})
        # Header strips span the full width, so smaller controls sitting inside
        # one (the return button) must win the hit test.
        for pass_collapse in (False, True):
            for button_id, rect in buttons.items():
                if button_id.startswith("collapse_") != pass_collapse:
                    continue
                if button_id == "back" and not self._layer_path:
                    continue
                if _point_in_rect(point, rect):
                    return button_id
        return ""

    def _inside_content(self, point):
        scale = self._layout.get("scale", 1.0) if self._layout else 1.0
        left, right = _editor_horizontal_bounds(self._region, _panel_anchor(self._region), scale)
        return left <= point[0] <= right and 0 <= point[1] < self._region.height

    def _begin_drag(self, context, record, source_index=-1, slot_index=-1):
        self._pending_source = source_index if source_index >= 0 else len(self._records) + slot_index
        self._source_slot = slot_index
        self._drag_record = copy.deepcopy(record)
        self._drag_profile = persistence._profile_key(context)
        self._drag_layer = self._layer_id()
        self._drag_slots = copy.deepcopy(self._slots)
        self._drag_source = -1
        self._press_mouse = self._mouse

    def _clear_removed_favorite(self, context, record, status):
        """After a native removal, drop the wheel copies of that favorite.

The favorites list merges several native menus and shows each action once,
so the same action can still be a favorite through another menu.  Only clear
the wheel when it is no longer in this context's favorites at all.
"""
        try:
            identity = native_bridge._action_identity(record)
            remaining = native_bridge._native_quick_favorites(context, force_refresh=True)
            if any(native_bridge._action_identity(item) == identity for item in remaining):
                return status + " " + T("It is still in another native Quick Favorites menu, so the wheel keeps it.")
        except Exception:
            # Without a trustworthy re-read, do not guess: keep the wheel as is.
            return status + " " + T("The other favorites could not be checked, so the wheel was not changed.")
        ok, message, cleared = persistence.remove_action_references(context, record)
        self._sync_layer(context)
        if not ok:
            return status + " " + T("But its copies on the wheel could not be cleared: {error}", error=message)
        if cleared:
            return status + " " + T("Also removed {count} slot(s) from wheels, sub-wheels and groups.", count=cleared)
        return status

    def _run_button(self, context, button_id):
        global _SELECTED_SOURCE, _SELECTED_SLOT, _STATUS
        if button_id.startswith("collapse_"):
            toggle_editor_collapsed(self._key[1], button_id[len("collapse_"):])
            self._layout = None
            _tag_view3d_redraw()
            return
        if button_id.startswith("edit_group_"):
            group_id = button_id[len("edit_group_"):]
            profile = persistence.load_profile(context)
            library = {"GROUPS": "groups", "FOLDERS": "folders"}.get(self._source_kind)
            if library is None or group_id not in profile[library]:
                _STATUS = T("The item changed; click it again")
                return
            if group_id in self._layer_path:
                self._layer_path = self._layer_path[:self._layer_path.index(group_id) + 1]
            else:
                self._layer_path.append(group_id)
            _SELECTED_SLOT = -1
            self._layout = None
            _STATUS = T("Editing this layer; drag items in from the sources")
            return
        if button_id in {"show_favorites", "show_folders", "show_groups", "show_shortcuts"}:
            self._source_kind = {"show_favorites": "FAVORITES", "show_folders": "FOLDERS", "show_groups": "GROUPS", "show_shortcuts": "SHORTCUTS"}[button_id]
            self._scroll = 0
            self._layout = None
            _SELECTED_SOURCE = 0
            self._confirm_remove_until = 0
            return
        if self._source_kind == "SHORTCUTS" and button_id in {"up", "manual", "down", "remove"}:
            if button_id == "up":
                bpy.ops.qclick2.capture_shortcut("INVOKE_DEFAULT")
                return
            if button_id == "manual":
                bpy.ops.qclick2.add_command("INVOKE_DEFAULT")
                return
            if not (0 <= _SELECTED_SOURCE < len(self._records)):
                return
            record = self._records[_SELECTED_SOURCE]
            is_command = bool(record.get("command_id"))
            key = record["command_id"] if is_command else record["shortcut_id"]
            source_kind = "COMMAND" if is_command else "SHORTCUT"
            if button_id == "down":
                bpy.ops.qclick2.rename_shortcut("INVOKE_DEFAULT", key=key, label=record["label"], kind=source_kind)
                return
            confirmation_key = source_kind + ":" + key
            if self._confirm_folder_id != confirmation_key or time.monotonic() > self._confirm_remove_until:
                self._confirm_folder_id = confirmation_key
                self._confirm_remove_until = time.monotonic() + 2.4
                _STATUS = T("Press again to remove the source; copies already on the wheel stay")
                return
            ok, message = (persistence.delete_command if is_command else persistence.delete_shortcut)(context, key)
            self._confirm_remove_until = 0
            self._layout = None
            _STATUS = T("Source removed; copies already on the wheel are unchanged") if ok else message
            return
        if button_id == "back":
            if self._layer_path:
                self._layer_path.pop()
            _SELECTED_SLOT = -1
            self._layout = None
            _STATUS = T("Editing this layer; drag items in from the sources") if self._layer_path else T("Back on the main wheel")
            return
        if self._source_kind in {"FOLDERS", "GROUPS"} and button_id in {"up", "down", "remove"}:
            kind = "GROUP" if self._source_kind == "GROUPS" else "FOLDER"
            if button_id == "up":
                bpy.ops.qclick2.folder_name("INVOKE_DEFAULT", operation="CREATE", folder_name=T("Group") if kind == "GROUP" else T("Sub-wheel"), kind=kind)
                return
            if not (0 <= _SELECTED_SOURCE < len(self._records)):
                return
            record = self._records[_SELECTED_SOURCE]
            folder_id = record["layer_id"]
            if button_id == "down":
                bpy.ops.qclick2.folder_name("INVOKE_DEFAULT", operation="RENAME", folder_id=folder_id, folder_name=record["label"], kind=kind)
                return
            if self._confirm_folder_id != folder_id or time.monotonic() > self._confirm_remove_until:
                self._confirm_folder_id = folder_id
                self._confirm_remove_until = time.monotonic() + 2.4
                _STATUS = T("Press again to delete \"{name}\" and all its links", name=record['label'])
                return
            ok, message = persistence.delete_folder(context, folder_id, kind)
            _STATUS = T("Deleted the item and its links") if ok else message
            self._confirm_remove_until = 0
            self._sync_layer(context)
            self._layout = None
            return
        if button_id in {"up", "down", "remove"}:
            if not (0 <= _SELECTED_SOURCE < len(self._records)):
                _STATUS = T("Select a favorite first")
                return
            record = self._records[_SELECTED_SOURCE]
            if button_id == "remove" and time.monotonic() > self._confirm_remove_until:
                self._confirm_remove_until = time.monotonic() + 2.4
                _STATUS = T("Press again to remove; this action is also cleared from wheels, sub-wheels and groups")
                return
            operation = {"up": "UP", "down": "DOWN", "remove": "REMOVE"}[button_id]
            try:
                _STATUS = native_bridge._mutate_native_action(context, record, operation)
                if operation == "REMOVE":
                    _STATUS = self._clear_removed_favorite(context, record, _STATUS)
                if operation == "UP" and _SELECTED_SOURCE > 0:
                    _SELECTED_SOURCE -= 1
                elif operation == "DOWN":
                    _SELECTED_SOURCE += 1
                self._confirm_remove_until = 0.0
                self._layout = None
            except native_bridge.NativeFavoritesError as exc:
                _STATUS = str(exc)
            except Exception as exc:
                _STATUS = T("Native Quick Favorites operation failed: {error}", error=exc)
            return
        if button_id == "clear":
            if not (0 <= _SELECTED_SLOT < len(self._slots)):
                _STATUS = T("Select a slot to clear first")
                return
            changed, message = persistence.clear_slot(context, _SELECTED_SLOT, layer_id=self._layer_id())
            _STATUS = T("Cleared the {slot} slot", slot=self._slot_name(_SELECTED_SLOT)) if changed else message
            self._layout = None
            return
        if button_id == "reset":
            self._cancel_drag(message=False)
            title = f"{self._layer_title} · {persistence.profile_label(context)}"
            bpy.ops.qclick2.reset_wheel("INVOKE_DEFAULT", layer_id=self._layer_id(), layer_title=title)
            self._layout = None

    def _cancel_drag(self, message=True):
        global _STATUS
        if message and (self._pending_source >= 0 or self._drag_source >= 0):
            _STATUS = T("Drag cancelled")
        self._pending_source = -1
        self._drag_source = -1
        self._source_slot = -1
        self._drag_record = None
        self._drag_slots = ()
        self._hover_slot = -1
        self._pressed_button = ""
        self._pressed_check = None

    def _resolve_live(self):
        """Re-resolve this session's area and region from live window data.

A Blender Region object is a raw pointer with no reference counting: changing
an area's editor type frees its regions without invalidating any Python object
still holding one, so reading a stored region afterwards dereferences freed
memory and takes Blender down with it.  Only regions reached by walking the
current window list are safe to touch, so refresh them every modal step and
treat a missing one as the end of the session.
"""
        try:
            windows = bpy.context.window_manager.windows
        except Exception:
            return None
        for window in windows:
            try:
                if int(window.as_pointer()) != self._key[0]:
                    continue
                screen = window.screen
            except (ReferenceError, AttributeError):
                return None
            if screen is None:
                return None
            for area in screen.areas:
                if int(area.as_pointer()) != self._key[1]:
                    continue
                if area.type not in persistence.SUPPORTED_AREAS:
                    return None
                for region in area.regions:
                    if region.type == "UI" and int(region.as_pointer()) == self._key[2]:
                        return (window, area, region)
                return None
        return None

    def _visible(self):
        return _panel_is_visible(self._key[1], self._key[2], self._region)

    def modal(self, context, event):
        global _SELECTED_SOURCE, _SELECTED_SLOT, _STATUS
        if getattr(self, "_finished", False):
            return {"CANCELLED"}
        if event.type == "MOUSEMOVE":
            self._tooltip_suspended = False
        elif event.type != "TIMER":
            self._tooltip_suspended = True
            self._tooltip.reset()
        if not _REGISTERED:
            self._finish()
            return {"CANCELLED"}

        live = self._resolve_live()
        if live is None:
            # The sidebar this session was drawing into is gone (the area was
            # switched to another editor, or closed).  Never touch the stored
            # handles again.
            self._finish()
            return {"CANCELLED"}
        self._window, self._area, self._region = live

        if event.type == "WINDOW_DEACTIVATE":
            self._cancel_drag()
            redraw_regions(self._area, ("UI",))
            return {"PASS_THROUGH"}

        if not self._visible():
            self._cancel_drag(message=False)
            return {"PASS_THROUGH"}

        self._mouse = self._local_mouse(event)
        if self._pending_source >= 0 and (persistence._profile_key(context) != self._drag_profile or self._layer_id() != self._drag_layer):
            self._cancel_drag()
            _STATUS = T("The mode changed; the drag was cancelled")
        inside_region = 0.0 <= self._mouse[0] <= self._region.width and 0.0 <= self._mouse[1] <= self._region.height
        inside_editor = bool(
            self._layout
            and not self._layout.get("too_small")
            and _point_in_rect(self._mouse, self._layout.get("editor"))
        )

        if self._pressed_check is not None:
            press = self._pressed_check
            if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
                press["cancelled"] = True
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}
            if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
                press["cancelled"] = True
                return {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE":
                if self._checkbox_at(self._mouse) != press["index"]:
                    press["cancelled"] = True
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                try:
                    changed, message = self._commit_checkbox(context, press)
                except Exception as exc:
                    changed, message = False, str(exc)
                _STATUS = (T("No longer recorded as last action; it still runs") if press["allowed"] else T("Now recorded as last action")) if changed else message
                self._cancel_drag(message=False)
                _tag_view3d_redraw()
                return {"RUNNING_MODAL"}
            # A checkbox gesture never starts a wheel, drag or native popup.
            return {"RUNNING_MODAL"}

        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            if self._pending_source >= 0 or self._drag_source >= 0:
                self._cancel_drag()
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}
            if event.type == "RIGHTMOUSE" and inside_editor:
                source_index = self._source_at(self._mouse)
                slot_index = self._slot_at(self._mouse)
                action = self._records[source_index] if source_index >= 0 else self._slots[slot_index] if 0 <= slot_index < len(self._slots) else None
                if action and action.get("kind", "ACTION") == "ACTION" and action.get("action_type", "OPERATOR") in {"OPERATOR", "PROPERTY", "MENU"}:
                    import json
                    self._cancel_drag(message=False)
                    bpy.ops.qclick2.repeat_settings("INVOKE_DEFAULT", payload=json.dumps(native_bridge._stored_action_entry(action), ensure_ascii=False))
                    return {"RUNNING_MODAL"}
            return {"PASS_THROUGH"}

        if event.type == "MOUSEMOVE":
            before = (self._hover_source, self._hover_button, self._hover_slot, self._drag_source)
            self._hover_source = self._source_at(self._mouse) if inside_region else -1
            self._hover_button = self._button_at(self._mouse) if inside_region else ""
            if self._pending_source >= 0 and self._drag_source < 0 and self._layout:
                distance = math.hypot(self._mouse[0] - self._press_mouse[0], self._mouse[1] - self._press_mouse[1])
                if distance >= 4.0 * self._layout.get("scale", 1.0):
                    self._drag_source = self._pending_source
                    _STATUS = T("Dragging: {name}", name=self._drag_record.get('label', T('Favorite')))
            self._hover_slot = self._slot_at(self._mouse) if self._drag_source >= 0 else -1
            dragging = self._pending_source >= 0 or self._drag_source >= 0
            after = (self._hover_source, self._hover_button, self._hover_slot, self._drag_source)
            # Every sidebar session sees every mouse move in the window, the
            # viewport included.  Redrawing unconditionally made each move over
            # the mesh also rebuild the whole sidebar, which stacks badly with
            # mouse-move-heavy tools such as PolyQuilt's preselect gizmo.  Only
            # redraw when something visible here changed, or a drag is live
            # (the ghost follows the pointer), or the tooltip may need to show.
            if dragging or after != before or inside_region:
                redraw_regions(self._area, ("UI",))
            return {"RUNNING_MODAL"} if dragging else {"PASS_THROUGH"}

        if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE"} and event.value == "PRESS":
            if (
                self._layout
                and not self._layout.get("too_small")
                and _point_in_rect(self._mouse, self._layout.get("favorite_content"))
            ):
                delta = -3 if event.type == "WHEELUPMOUSE" else 3
                self._scroll = max(0, min(self._layout.get("max_scroll", 0), self._scroll + delta))
                self._layout = None
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "PRESS" and inside_editor:
            check_index = self._checkbox_at(self._mouse)
            if check_index >= 0:
                self._cancel_drag(message=False)
                action = copy.deepcopy(self._records[check_index])
                key = _repeat_checkbox_key(action)
                self._pressed_check = dict(index=check_index, key=key, action=action,
                                           profile=persistence._profile_key(context), sources=self._source_kind,
                                           scroll=self._scroll, allowed=persistence.repeat_allowed(context, action),
                                           cancelled=False)
                self._confirm_remove_until = 0
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}
            button_id = self._button_at(self._mouse)
            if button_id:
                self._pressed_button = button_id
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}
            source_index = self._source_at(self._mouse)
            if source_index >= 0:
                if source_index != _SELECTED_SOURCE:
                    self._confirm_remove_until = 0
                _SELECTED_SOURCE = source_index
                self._begin_drag(context, self._records[source_index], source_index=source_index)
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}
            slot_index = self._slot_at(self._mouse)
            if slot_index >= 0:
                _SELECTED_SLOT = slot_index
                if slot_index < len(self._slots) and self._slots[slot_index]:
                    self._begin_drag(context, self._slots[slot_index], slot_index=slot_index)
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE" and self._drag_source >= 0:
            slot_index = self._slot_at(self._mouse)
            if self._drag_record and 0 <= slot_index < len(self._slots):
                record = self._drag_record
                replaced = self._slots[slot_index] if slot_index < len(self._slots) else None
                try:
                    if self._source_slot >= 0:
                        changed, message = persistence.move_slot(context, self._source_slot, slot_index, self._drag_slots, layer_id=self._drag_layer)
                    else:
                        entry = copy.deepcopy(record) if record.get("kind") in persistence.LIBRARIES or record.get("shortcut_id") or record.get("command_id") else native_bridge._stored_action_entry(record)
                        changed, message = persistence.set_slot(context, slot_index, entry, expected=self._drag_slots, layer_id=self._drag_layer)
                except Exception as exc:
                    changed, message = (False, str(exc))
                label = record.get("label", T("Favorite"))
                if changed:
                    _SELECTED_SLOT = slot_index
                    if replaced:
                        replaced_label = replaced.get("label", T("previous item")) if isinstance(replaced, dict) else str(replaced)
                        _STATUS = T("{slot}: {old} → {new}", slot=self._slot_name(slot_index), old=replaced_label, new=label)
                    else:
                        _STATUS = T("Placed {name} in {slot}", slot=self._slot_name(slot_index), name=label)
                else:
                    _STATUS = T("{name}: {error}", name=persistence.profile_label(context), error=message or T("Cannot write to this layer"))
                    self.report({"WARNING"}, _STATUS)
            else:
                _STATUS = T("Not dropped on a slot; layout unchanged")
            self._cancel_drag(message=False)
            redraw_regions(self._area, ("UI",))
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            released_button = self._button_at(self._mouse)
            pressed_button = self._pressed_button
            self._cancel_drag(message=False)
            if released_button and released_button == pressed_button:
                self._run_button(context, released_button)
                redraw_regions(self._area, ("UI",))
                return {"RUNNING_MODAL"}

        return {"PASS_THROUGH"}

    # -- drawing ------------------------------------------------------------

    def _draw(self):
        global _SELECTED_SOURCE
        context = bpy.context
        if context.area is None or context.region is None:
            return
        if int(context.area.as_pointer()) != self._key[1] or int(context.region.as_pointer()) != self._key[2]:
            return
        # Adopt the region Blender handed us; the stored one is only ever a
        # pointer and must never be the thing we read sizes from.
        self._area, self._region = context.area, context.region
        if not self._visible():
            return

        theme = _editor_theme(self._region)
        profile = self._sync_layer(context)
        self._display_profile = profile
        self._repeat_exclusions = frozenset(profile["repeat_exclusions"])
        self._group_mode = self._layer_id() in profile["groups"]
        self._records, self._slots = _ui_snapshot(context, self._layer_id(), self._source_kind, profile)
        self._layer_title = " / ".join([T("Main Wheel"), *(persistence._layer(profile, key)["label"] for key in self._layer_path)])
        if self._records:
            _SELECTED_SOURCE = max(0, min(_SELECTED_SOURCE, len(self._records) - 1))
        else:
            _SELECTED_SOURCE = 0
        anchor = _panel_anchor(self._region)
        self._layout = _editor_layout(self._region, anchor, len(self._records), self._scroll, theme,
                                      self._group_mode, self._source_kind == "SHORTCUTS",
                                      collapsed=editor_collapsed(self._key[1]))
        self._scroll = int(self._layout.get("scroll", self._scroll))
        scale = self._layout.get("scale", 1.0)
        previous_scissor = gpu.state.scissor_get()
        try:
            gpu.state.blend_set("ALPHA")
            vx, vy, vw, vh = gpu.state.viewport_get()
            content_left, content_right = _editor_horizontal_bounds(self._region, anchor, scale)
            left = max(0, math.ceil(content_left))
            right = min(vw, math.floor(content_right))
            gpu.state.scissor_set(vx + left, vy, max(0, right - left), vh)
            gpu.state.scissor_test_set(True)
            if self._layout.get("too_small"):
                notice = self._layout["notice"]
                _draw_widget(notice, theme["panel"], theme)
                _draw_text(T("Widen the sidebar to show the editor"), notice, EDITOR_FONT_SIZE * scale, theme["wcol"]["regular"]["text"], padding=EDITOR_TEXT_PADDING * scale)
                return
            self._draw_editor(theme, scale, persistence.profile_label(context))
            self._draw_hover_tip(theme, (left + 2, 4, right - 2, vh - 4))
        finally:
            try:
                gpu.state.scissor_set(*previous_scissor)
                gpu.state.scissor_test_set(False)
                gpu.state.blend_set("NONE")
            except Exception:
                pass

    def _draw_hover_tip(self, theme, bounds):
        if self._tooltip_suspended or self._pending_source >= 0 or self._drag_source >= 0 or self._pressed_check is not None:
            self._tooltip.reset()
            return
        source, slot = self._source_at(self._mouse), self._slot_at(self._mouse)
        entry = self._records[source] if source >= 0 else self._slots[slot] if slot >= 0 else None
        button = self._button_at(self._mouse)
        if button.startswith("edit_group_"):
            content = (T("Edit sub-wheel") if self._source_kind == "FOLDERS" else T("Edit group"),)
        elif button == "reset":
            content = (T("Reset"), T("Clears every slot of the current wheel; type {word} to confirm", word=RESET_CONFIRM_WORD))
        else:
            content = _tip_content(entry, self._display_profile)
        key = (self._edit_profile, self._layer_id(), self._source_kind, source, slot, content, bounds, self._scroll) if content else None
        if self._tooltip.ready(key, self._mouse):
            _draw_tip(content, self._mouse, bounds, theme)

    def _draw_editor(self, theme, scale, mode_label):
        wcol_tool = theme["wcol"]["tool"]
        wcol_list = theme["wcol"]["list"]
        wcol_pie = theme["wcol"]["pie"]
        wcol_scroll = theme["wcol"]["scroll"]
        heading_color = theme["wcol"]["regular"]["text"]

        # Cover gaps, empty lists and the status footer before translucent
        # native widget states are composited. This follows View2D scrolling.
        backdrop = self._layout["editor"]
        gpu.state.blend_set("NONE")
        try:
            # Match the native panel silhouette: square corners here read as a
            # different kind of surface next to Blender's own rounded panels.
            # Native panels round off about half as tightly as a box widget;
            # a wider corner reads as the right edge stopping short.
            backdrop_radius = _widget_radius(theme["panel"], theme, backdrop) * 0.5
            _draw_flat_fill(backdrop, _rounded_points(backdrop, backdrop_radius),
                            theme["panel"]["inner"])
        finally:
            gpu.state.blend_set("ALPHA")

        # Each half is its own panel, header first, the way Blender stacks them.
        self._draw_panel_header(theme, scale, self._layout["wheel_header"],
                                f"{self._layer_title} · {mode_label}", heading_color,
                                self._layout["wheel_collapsed"], "collapse_wheel")
        self._draw_panel_header(theme, scale, self._layout["sources_header"],
                                T(_SOURCE_SECTION_TITLES.get(self._source_kind, "Favorites")), heading_color,
                                self._layout["sources_collapsed"], "collapse_sources")

        self._draw_slots(theme, wcol_pie, scale)
        self._draw_favorite_rows(theme, wcol_list, scale)
        self._draw_scrollbar(wcol_scroll, theme)
        self._draw_buttons(theme, wcol_tool, scale)
        self._draw_status(theme, scale)
        self._draw_ghost(theme, wcol_tool, scale)

    def _draw_panel_header(self, theme, scale, header, title, heading_color, collapsed, button_id):
        """Native panel header: a stroked chevron then the title.

Blender draws the arrow as two short strokes meeting at a point, about 9x5
unscaled pixels, not as a solid triangle.
"""
        if not header:
            return
        hovered = self._hover_button == button_id
        color = theme["wcol"]["regular"]["text"] if hovered else heading_color

        half_width = 4.5 * scale
        half_height = 2.5 * scale
        cx = header[0] + 15.0 * scale
        cy = (header[1] + header[3]) * 0.5
        width = max(1.0, 1.3 * scale)
        if collapsed:
            tip = (cx + half_height, cy)
            _draw_line((cx - half_height, cy + half_width), tip, color, width)
            _draw_line(tip, (cx - half_height, cy - half_width), color, width)
        else:
            tip = (cx, cy - half_height)
            _draw_line((cx - half_width, cy + half_height), tip, color, width)
            _draw_line(tip, (cx + half_width, cy + half_height), color, width)

        right_pad = 46.0 * scale if (button_id == "collapse_wheel" and self._layer_path) else 8.0 * scale
        title_rect = (header[0] + 24.0 * scale, header[1], header[2] - right_pad, header[3])
        _draw_text(title, title_rect, EDITOR_FONT_SIZE * scale, color, align="LEFT", padding=EDITOR_TEXT_PADDING * scale)

    def _draw_slots(self, theme, wcol_pie, scale):
        for slot_index, rect in enumerate(self._layout["slot_rects"]):
            entry = self._slots[slot_index] if slot_index < len(self._slots) else None
            if isinstance(entry, dict):
                entry_label = persistence.display_label(entry, self._display_profile)
                if entry.get("kind") == "FOLDER":
                    entry_label = "> " + entry_label
                elif entry.get("kind") == "GROUP":
                    entry_label = "+ " + entry_label
            else:
                entry_label = str(entry or "")
            dropping = self._drag_source >= 0 and self._hover_slot == slot_index
            selected = _SELECTED_SLOT == slot_index
            if dropping:
                value = (T("Swap") if self._source_slot >= 0 else T("Replace")) if entry_label else T("Drop")
            else:
                value = entry_label or T("Empty")
            if self._group_mode:
                value = f"{slot_index + 1} · {value}"
            state = "selected" if dropping or selected else "normal"
            style = _widget_style(wcol_pie, state, "pie")
            fill = (*style["inner"][:3], 1.0)
            if not entry_label and not (dropping or selected):
                fill = _blend(theme["panel"]["inner"], fill, 0.55, alpha=1.0)
            _draw_widget(
                rect,
                wcol_pie,
                theme,
                state=state,
                role="pie",
                fill=fill,
            )
            text_color = wcol_pie["text_sel"] if dropping or selected else wcol_pie["text"]
            if not entry_label and not (dropping or selected):
                text_color = (text_color[0], text_color[1], text_color[2], text_color[3] * 0.55)
            _draw_text(value, rect, EDITOR_FONT_SIZE * scale, text_color,
                       align="LEFT" if self._group_mode else "CENTER",
                       padding=(2 * EDITOR_TEXT_PADDING if self._group_mode else EDITOR_TEXT_PADDING) * scale)

    def _draw_favorite_rows(self, theme, wcol_list, scale):
        if self._layout.get("sources_collapsed"):
            return
        for source_index, rect in self._layout["source_rects"]:
            if source_index >= len(self._records):
                continue
            record = self._records[source_index]
            selected = source_index == _SELECTED_SOURCE
            hovered = source_index == self._hover_source
            dragging = source_index == self._drag_source
            row_state = "selected_hover" if selected and hovered else "selected" if selected else "hover" if hovered else "normal"
            palette = _widget_style(wcol_list, row_state, "list")
            # Precompose native translucent states into an opaque surface.
            background = theme["panel"]["inner"]
            fill = _blend(background, palette["inner"], palette["inner"][3], alpha=1.0)
            if dragging:
                fill = _blend(background, fill, 0.30, alpha=1.0)
            if fill is not None and fill[3] > 0.0:
                _draw_widget(rect, palette, theme, fill=fill, border=(0.0, 0.0, 0.0, 0.0))
            text_color = palette["text"]
            if dragging:
                text_color = (text_color[0], text_color[1], text_color[2], text_color[3] * 0.45)
            repeat_key = _repeat_checkbox_key(record)
            label_rect, check_hit, check_rect = _source_row_parts(rect, scale, bool(repeat_key))
            button_id = "edit_group_" + record.get("layer_id", "")
            edit_rect = self._group_edit_buttons().get(button_id)
            if edit_rect:
                label_rect, _hit, _glyph = _source_row_parts(rect, scale, True)
            label = persistence.display_label(record, self._display_profile)
            if self._source_kind == "SHORTCUTS":
                from .shortcuts import shortcut_text
                label += " · " + T("Command") if record.get("command_id") else " · " + shortcut_text(record.get("recorded_shortcut", {}))
            _draw_text(label, label_rect, EDITOR_FONT_SIZE * scale, text_color, align="LEFT", padding=EDITOR_TEXT_PADDING * scale)
            if edit_rect:
                state = "pressed" if self._pressed_button == button_id else "hover" if self._hover_button == button_id else "normal"
                palette = theme["wcol"]["tool"]
                style = _widget_style(palette, state)
                _draw_widget(edit_rect, palette, theme, state=state, fill=(*style["inner"][:3], 1.0))
                x, y = _rect_center(edit_rect)
                color = style["text"]
                # Small outlined pencil, independent of installed font glyphs.
                points = [(-4, -4), (-3, -1), (2, 4), (4, 2), (-1, -3), (-4, -4)]
                for a, b in zip(points, points[1:]):
                    _draw_line((x + a[0] * scale, y + a[1] * scale),
                               (x + b[0] * scale, y + b[1] * scale), color, scale)

            if check_rect is not None:
                option = theme["wcol"]["option"]
                checked = repeat_key not in self._repeat_exclusions
                hovered_check = _point_in_rect(self._mouse, check_hit)
                state = "selected" if checked else "hover" if hovered_check else "normal"
                _draw_widget(check_rect, option, theme, state=state, emboss=True)
                if checked:
                    x, y = _rect_center(check_rect)
                    color = option["item"]
                    _draw_line((x - 4 * scale, y), (x - scale, y - 3 * scale), color, 1.8 * scale)
                    _draw_line((x - scale, y - 3 * scale), (x + 4 * scale, y + 3 * scale), color, 1.8 * scale)

        if not self._records:
            message = T("Capture a shortcut, or press Command to add one") if self._source_kind == "SHORTCUTS" else T("Press New to create a group") if self._source_kind == "GROUPS" else T("Press New to create a sub-wheel") if self._source_kind == "FOLDERS" else _READ_ERROR or T("No native Quick Favorites in this mode")
            _draw_text(message, self._layout["favorite_content"], EDITOR_FONT_SIZE * scale, theme["wcol"]["regular"]["text"], padding=EDITOR_TEXT_PADDING * scale)

    def _draw_scrollbar(self, wcol_scroll, theme):
        if self._layout.get("sources_collapsed") or self._layout["max_scroll"] <= 0:
            return
        content = self._layout["favorite_content"]
        frame = self._layout["favorite_frame"]
        scale = self._layout["scale"]
        track_width = self._layout["scrollbar_width"]
        inset = 2.0 * scale
        track = (frame[2] - track_width - inset, content[1], frame[2] - inset, content[3])
        track_points = _rounded_points(track, track_width * 0.5)
        # Blender ships wcol_scroll.inner fully transparent (the native track
        # only shows on hover), which left an empty gutter here.  Precompose it
        # onto the panel like every other body in this editor; when the theme's
        # track has no alpha at all, derive a faint lane from the thumb colour.
        background = theme["panel"]["inner"]
        track_inner = wcol_scroll["inner"]
        if track_inner[3] >= 0.05:
            track_color = _blend(background, track_inner, track_inner[3], alpha=1.0)
        else:
            track_color = _blend(background, wcol_scroll["item"], 0.18, alpha=1.0)
        _draw_flat_fill(track, track_points, track_color)
        track_height = track[3] - track[1]
        visible = self._layout["visible_rows"]
        thumb_height = max(18.0 * scale, track_height * visible / max(1, len(self._records)))
        travel = track_height - thumb_height
        amount = self._scroll / max(1, self._layout["max_scroll"])
        thumb_top = track[3] - travel * amount
        thumb = (track[0], thumb_top - thumb_height, track[2], thumb_top)
        thumb_points = _rounded_points(thumb, track_width * 0.5)
        thumb_item = wcol_scroll["item"]
        _draw_flat_fill(thumb, thumb_points,
                        _blend(background, thumb_item, thumb_item[3], alpha=1.0))

    def _draw_buttons(self, theme, wcol_tool, scale):
        now = time.monotonic()
        button_labels = {
            "clear": T("Clear Slot"),
            "reset": "",
            "up": T("Up"),
            "down": T("Down"),
            "remove": T("Remove?") if now <= self._confirm_remove_until else T("Remove"),
            "back": T("Back"),
            "show_favorites": T("Favorites"),
            "show_folders": T("Sub-wheels"),
            "show_groups": T("Groups"),
            "show_shortcuts": T("Library"),
            "manual": T("Command"),
        }
        if self._source_kind in {"FOLDERS", "GROUPS"}:
            button_labels.update(up=T("New"), down=T("Rename"), remove=T("Delete?") if now <= self._confirm_remove_until else T("Delete"))
        elif self._source_kind == "SHORTCUTS":
            button_labels.update(up=T("Capture"), down=T("Rename"), remove=T("Confirm") if now <= self._confirm_remove_until else T("Remove"))
        has_selection = 0 <= _SELECTED_SOURCE < len(self._records)
        for button_id, rect in self._layout["buttons"].items():
            if button_id.startswith("collapse_"):
                continue
            if button_id == "back" and not self._layer_path:
                continue
            hovered = button_id == self._hover_button
            pressed = button_id == self._pressed_button
            confirming = button_id == "remove" and now <= self._confirm_remove_until
            disabled = bool(button_id in {"up", "down", "remove"} and not has_selection and not (button_id == "up" and self._source_kind in {"FOLDERS", "GROUPS", "SHORTCUTS"}))
            active_tab = button_id == {"FOLDERS": "show_folders", "GROUPS": "show_groups", "FAVORITES": "show_favorites", "SHORTCUTS": "show_shortcuts"}[self._source_kind]
            if pressed or confirming or active_tab:
                state = "pressed"
            elif hovered and not disabled:
                state = "hover"
            else:
                state = "normal"
            style = _widget_style(wcol_tool, state)
            fill = (*style["inner"][:3], 1.0)
            if disabled:
                # Retain a muted native appearance without a transparent body.
                fill = _blend(theme["panel"]["inner"], style["inner"], 0.4, alpha=1.0)
            _draw_widget(rect, wcol_tool, theme, state=state, fill=fill)
            text_color = _widget_style(wcol_tool, state)["text"]
            if disabled:
                text_color = (text_color[0], text_color[1], text_color[2], text_color[3] * 0.45)
            if style["show_shaded"]:
                bg_top = _shade(fill, style["shadetop"])
                bg_bottom = _shade(fill, style["shadedown"])
            else:
                bg_top = bg_bottom = fill
            if button_id == "reset":
                _draw_icon_reset(_rect_center(rect), text_color, scale, bg_top, bg_bottom)
            elif button_id == "clear":
                self._draw_icon_label(rect, _draw_icon_x, button_labels[button_id], text_color, scale, bg_top, bg_bottom)
            else:
                _draw_text(button_labels[button_id], rect, EDITOR_FONT_SIZE * scale, text_color, padding=EDITOR_TEXT_PADDING * scale)

    def _draw_icon_label(self, rect, draw_icon, label, color, scale, bg_top=None, bg_bottom=None):
        """Icon then label, centred together the way a native icon button is."""
        icon_width = ICON_INK_WIDTH["X"] * scale
        gap = EDITOR_TEXT_PADDING * scale
        padding = EDITOR_TEXT_PADDING * scale
        blf.size(0, max(8, int(round(EDITOR_FONT_SIZE * scale))))
        available = rect[2] - rect[0] - 2 * padding - icon_width - gap
        text_width = blf.dimensions(0, _fit_text_px(0, label, available))[0] if available > 4 else 0.0
        group_width = icon_width + (gap + text_width if text_width else 0.0)
        left = (rect[0] + rect[2] - group_width) * 0.5
        draw_icon((left + icon_width * 0.5, (rect[1] + rect[3]) * 0.5), color, scale, bg_top, bg_bottom)
        if text_width:
            text_left = left + icon_width + gap
            _draw_text(label, (text_left, rect[1], text_left + text_width + 1.0, rect[3]),
                       EDITOR_FONT_SIZE * scale, color, align="LEFT", padding=0)

    def _draw_status(self, theme, scale):
        status_rect = self._layout["status"]
        color = theme["wcol"]["regular"]["text"]
        color = (color[0], color[1], color[2], color[3] * 0.8)
        _draw_text(persistence.load_error() or _STATUS or T(DEFAULT_STATUS), status_rect, EDITOR_STATUS_FONT_SIZE * scale, color, align="LEFT",
                   padding=EDITOR_TEXT_PADDING * scale)

    def _draw_ghost(self, theme, wcol_tool, scale):
        if self._drag_source < 0 or not self._drag_record:
            return
        label = persistence.display_label(self._drag_record, self._display_profile)
        ghost_width = min(150 * scale, self._region.width - 18 * scale)
        ghost_height = 24 * scale
        cx = self._mouse[0] + 16 * scale
        cy = self._mouse[1] - 14 * scale
        margin = 2.0 * scale
        cx = max(ghost_width * 0.5 + margin, min(cx, self._region.width - ghost_width * 0.5 - margin))
        cy = max(ghost_height * 0.5 + margin, min(cy, self._region.height - ghost_height * 0.5 - margin))
        ghost = _rect_from_center(cx, cy, ghost_width, ghost_height)
        style = _widget_style(wcol_tool, "pressed")
        _draw_widget(ghost, wcol_tool, theme, state="pressed", fill=(*style["inner"][:3], 1.0))
        _draw_text(label, ghost, EDITOR_FONT_SIZE * scale, wcol_tool["text_sel"], padding=EDITOR_TEXT_PADDING * scale)

    def _finish(self):
        self._finished = True
        self._attached = None
        if self._draw_handle is not None:
            try:
                (self._draw_space or bpy.types.SpaceView3D).draw_handler_remove(self._draw_handle, "UI")
            except Exception:
                pass
        self._draw_handle = None
        if self._key is not None and _SESSIONS.get(self._key) is self:
            _SESSIONS.pop(self._key, None)
        # _finish runs exactly when the area may already be gone, so resolve it
        # by pointer instead of touching the handle captured at invoke time.
        try:
            redraw_area(self._key[1] if self._key else 0, ("UI",))
        except Exception:
            pass


def _region_contains(region, event):
    return (region.x <= event.mouse_x < region.x + region.width
            and region.y <= event.mouse_y < region.y + region.height)


_SHIM_IDNAMES = {"QCLICK2_OT_editor_session", "qclick2.editor_session"}


def _modal_idnames(window):
    try:
        return [str(op.bl_idname) for op in window.modal_operators]
    except Exception:
        return None


def _other_modal_running(window):
    """Attaching on top of a running modal (transform, the wheel, a capture)
    would put the editor ahead of it in the handler queue."""
    names = _modal_idnames(window)
    return bool(names) and any(name not in _SHIM_IDNAMES for name in names)


class QCLICK2_OT_editor_session(Operator):
    """Input shim: attached while the pointer is over a Q&Click sidebar."""
    bl_idname = "qclick2.editor_session"
    bl_label = "Q&Click Editor Session"
    bl_options = {"INTERNAL"}

    from_monitor: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    _key = None

    def invoke(self, context, event):
        global _KEYMAP_WAKE_SEEN
        if not self.from_monitor:
            _KEYMAP_WAKE_SEEN = True
        if not _REGISTERED or context.window is None or context.area is None or context.region is None:
            return {"CANCELLED", "PASS_THROUGH"}
        if context.region.type != "UI":
            return {"CANCELLED", "PASS_THROUGH"}
        session = _SESSIONS.get(_session_key(context.window, context.area, context.region))
        if (session is None or session._finished or session._attached is not None
                or not session._visible() or not _region_contains(context.region, event)
                or _other_modal_running(context.window)):
            return {"CANCELLED", "PASS_THROUGH"}
        self._key = session._key
        session._attached = self
        context.window_manager.modal_handler_add(self)
        redraw_regions(context.area, ("UI",))
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        session = _SESSIONS.get(self._key)
        if session is None or session._attached is not self:
            return {"CANCELLED", "PASS_THROUGH"}
        try:
            result = session.modal(context, event)
        except Exception:
            # An editor bug must never leave a modal swallowing input.
            session._cancel_drag(message=False)
            session.detach()
            raise
        if session._finished or session._attached is not self:
            return {"CANCELLED", "PASS_THROUGH"}
        if "RUNNING_MODAL" in result:
            return result
        if "CANCELLED" in result or "FINISHED" in result:
            session.detach()
            return {"CANCELLED", "PASS_THROUGH"}
        region = session._region
        if session.idle() and (event.type == "WINDOW_DEACTIVATE" or not _region_contains(region, event)
                               or not session._visible()):
            session.detach()
            redraw_regions(session._area, ("UI",))
            return {"CANCELLED", "PASS_THROUGH"}
        return result

    def cancel(self, _context):
        session = _SESSIONS.get(self._key)
        if session is not None and session._attached is self:
            session.detach()


# ---------------------------------------------------------------------------
# Placeholder panel and session monitor
# ---------------------------------------------------------------------------


class _EditorPanelBase:
    """A Blender panel is bound to one space type, so each supported editor
needs its own registered subclass sharing this draw."""

    bl_label = "Q&Click Editor"
    bl_region_type = "UI"
    bl_category = "Q&Click"
    bl_options = {"HIDE_HEADER"}

    def draw(self, context):
        area_pointer = int(context.area.as_pointer())
        _PANEL_HEARTBEATS[area_pointer] = {
            "region": int(context.region.as_pointer()),
            "seen": time.monotonic(),
        }
        theme = _editor_theme(context.region)
        collapsed = editor_collapsed(area_pointer)
        geometry = _editor_layout(context.region, _panel_anchor(context.region), 0, 0, theme,
                                  collapsed=collapsed)
        bounds = geometry.get("editor", geometry.get("notice"))
        reserved = (bounds[3] - bounds[1]) / theme["scale"] + 2.0
        reserve_rows = max(2, int(math.ceil(reserved / PANEL_ROW_HEIGHT)))
        column = self.layout.column(align=True)
        for _index in range(reserve_rows):
            column.label(text="")


class QCLICK2_PT_editor(_EditorPanelBase, Panel):
    bl_idname = "QCLICK2_PT_editor"
    bl_space_type = "VIEW_3D"


class QCLICK2_PT_editor_image(_EditorPanelBase, Panel):
    bl_idname = "QCLICK2_PT_editor_image"
    bl_space_type = "IMAGE_EDITOR"


CLASSES = (
    QCLICK2_OT_folder_name,
    QCLICK2_OT_reset_wheel,
    QCLICK2_OT_editor_session,
    QCLICK2_PT_editor,
    QCLICK2_PT_editor_image,
)


MONITOR_INTERVAL = 0.05
MONITOR_HOUSEKEEPING_TICKS = 4  # 0.2s: session upkeep and heartbeat redraws


def _category_shown(area, region):
    """The Q&Click tab is the one on screen in this sidebar."""
    try:
        if not area.spaces.active.show_region_ui:
            return False
    except (AttributeError, ReferenceError):
        return False
    try:
        return region.active_panel_category == _EditorPanelBase.bl_category
    except AttributeError:
        return True
    except ReferenceError:
        return False


def _wake_session(window, screen, area, region, session):
    """Attach the input shim when the pointer is already over the sidebar."""
    if session._attached is not None:
        # Blender can drop a modal handler without calling cancel(); never let
        # a lost shim keep the sidebar deaf for the rest of the session.
        names = _modal_idnames(window)
        if names is None or any(name in _SHIM_IDNAMES for name in names):
            return
        session.detach()
    if session._finished or _other_modal_running(window):
        return
    if _KEYMAP_WAKE_SEEN:
        # The wake keymap only fires when nothing above the sidebar (a popup
        # dialog, a menu, another modal) has taken the event.  A timer cannot
        # see popups, and attaching above one would steal its clicks, so the
        # timer only stands in until the keymap has proven itself.
        return
    if not _panel_is_visible(session._key[1], session._key[2], region):
        return
    try:
        with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
            bpy.ops.qclick2.editor_session("INVOKE_DEFAULT", from_monitor=True)
    except Exception:
        pass


def _sidebar_monitor():
    global _MONITOR_TICKS
    if not _REGISTERED:
        return None

    _MONITOR_TICKS += 1
    housekeeping = _MONITOR_TICKS % MONITOR_HOUSEKEEPING_TICKS == 0
    # Periodically drop the native cache so favorites added or removed through
    # Blender's own Quick Favorites menu show up without a manual refresh.
    if _MONITOR_TICKS % (NATIVE_REFRESH_TICKS * MONITOR_HOUSEKEEPING_TICKS) == 0:
        try:
            native_bridge._invalidate_native_cache()
        except Exception:
            pass

    live_keys = set()
    try:
        windows = list(bpy.context.window_manager.windows)
    except Exception:
        windows = []
    for window in windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type not in persistence.SUPPORTED_AREAS:
                continue
            region = next((item for item in area.regions if item.type == "UI"), None)
            if region is None:
                continue
            key = _session_key(window, area, region)
            live_keys.add(key)
            session = _SESSIONS.get(key)
            if session is None:
                try:
                    session = _EditorSession()
                    session.start(window, area, region)
                except Exception:
                    log.exception("Q&Click sidebar editor could not start")
                    continue
            shown = _category_shown(area, region)
            if shown:
                _wake_session(window, screen, area, region, session)
            # Keep the sidebar's heartbeat ticking, but only where the Q&Click
            # tab is on screen; other tabs and the viewport are left alone.
            if housekeeping and shown:
                redraw_regions(area, ("UI",))

    for key, session in tuple(_SESSIONS.items()):
        if key not in live_keys:
            session._finish()
    if housekeeping:
        now = time.monotonic()
        for area_pointer, heartbeat in tuple(_PANEL_HEARTBEATS.items()):
            if now - heartbeat["seen"] > 2.0:
                _PANEL_HEARTBEATS.pop(area_pointer, None)

        # Collapsed state is keyed by area pointer; drop entries whose area is
        # gone so a freed address cannot hand its state to an unrelated area.
        live_areas = {key[1] for key in live_keys}
        for area_pointer in tuple(_EDITOR_COLLAPSED):
            if area_pointer not in live_areas:
                _EDITOR_COLLAPSED.pop(area_pointer, None)
    return MONITOR_INTERVAL


_KEYMAP_ITEMS = []
_KEYMAP_WAKE_SEEN = False


def _register_wake_keymap():
    """Attach the editor the moment the pointer moves over a sidebar.

The monitor timer alone would leave up to one tick of lag after the pointer
enters, enough to drop a quick first click.  Sidebar panel regions run the
"View2D Buttons List" keymap; the shim's invoke passes the event through
untouched everywhere except an idle Q&Click sidebar.
"""
    try:
        keyconfig = bpy.context.window_manager.keyconfigs.addon
    except AttributeError:
        return
    if keyconfig is None:
        return
    keymap = keyconfig.keymaps.new(name="View2D Buttons List", space_type="EMPTY", region_type="WINDOW")
    item = keymap.keymap_items.new("qclick2.editor_session", "MOUSEMOVE", "ANY")
    _KEYMAP_ITEMS.append((keymap, item))


def _unregister_wake_keymap():
    global _KEYMAP_WAKE_SEEN
    _KEYMAP_WAKE_SEEN = False
    for keymap, item in _KEYMAP_ITEMS:
        try:
            keymap.keymap_items.remove(item)
        except Exception:
            pass
    _KEYMAP_ITEMS.clear()


def register():
    global _REGISTERED
    _REGISTERED = True
    _register_wake_keymap()
    if clear_sessions not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(clear_sessions)
    if not bpy.app.timers.is_registered(_sidebar_monitor):
        bpy.app.timers.register(_sidebar_monitor, first_interval=MONITOR_INTERVAL, persistent=True)


@persistent
def clear_sessions(_unused=None):
    """Drop all editor handles before Blender replaces its screens."""
    global _UNIFORM_SHADER, _SMOOTH_SHADER, _SELECTED_SOURCE, _SELECTED_SLOT
    for session in tuple(_SESSIONS.values()):
        session._finish()
    _SESSIONS.clear()
    _PANEL_HEARTBEATS.clear()
    _EDITOR_COLLAPSED.clear()
    _SELECTED_SOURCE, _SELECTED_SLOT = 0, -1
    native_bridge._invalidate_native_cache()
    _UNIFORM_SHADER = None
    _SMOOTH_SHADER = None


def unregister():
    global _REGISTERED
    _REGISTERED = False
    _unregister_wake_keymap()
    if clear_sessions in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(clear_sessions)
    try:
        if bpy.app.timers.is_registered(_sidebar_monitor):
            bpy.app.timers.unregister(_sidebar_monitor)
    except Exception:
        pass
    clear_sessions()
