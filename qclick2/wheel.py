# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Q&Click main wheel runtime (GPU overlay).

Port of the proven Q&Click 1.x modal wheel, first-version scope: hold the
trigger key, slide toward a direction, release to execute.  Includes the 1.x
semantic sector model (empty slots are absorbed by occupied neighbours, with
boundary hysteresis), the adaptive first-layer card layout, and the universal
source-release handoff that keeps the selecting release event from leaking
into whatever UI the chosen action opens.

Visuals draw with the same native-theme widget language as the sidebar editor
(wcol_pie_menu), so wheel cards look exactly like the editor's slot pills.
"""

import copy
import math
import time
from functools import lru_cache

import blf
import bpy
from bpy.app.handlers import persistent
from .runtime import redraw_area, space_class
import gpu
from bpy.props import StringProperty
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader

from . import groups, persistence
from .i18n import T
from .editor import (
    _draw_widget,
    _fit_text_px,
    _native_theme,
    _HoverTip,
    _tip_content,
    _draw_tip,
)
from .dispatch import (
    QFAV_ACTION_HANDOFF_TIMER_INTERVAL,
    QFAV_GENERAL_ACTION_HANDOFF_DELAY,
    QFAV_NATIVE_UI_HANDOFF_DELAY,
    _action_needs_pointer_release_barrier,
    _schedule_action_entry,
    _last_action_for_context,
    _repeat_context_key,
)
from .native_bridge import _action_identity, _canonicalize_action

# --- geometry constants (1.x values, unscaled pixels like the 1.x overlay) ---

QFAV_DIRECTION_DEADZONE = 34
QFAV_CENTER_RADIUS = 22
QCLICK_CENTER_RING_INNER_RADIUS = 14

QCLICK_ROOT_CARDINAL_X = 132
QCLICK_ROOT_CARDINAL_Y = 106
QCLICK_ROOT_DIAGONAL_X = 96
QCLICK_ROOT_DIAGONAL_Y = 68
QCLICK_ROOT_DIAGONAL_OUTWARD_STEP = 8
QCLICK_ROOT_DIAGONAL_OUTWARD_MAX = 80
QCLICK_ROOT_CARD_GAP = 12
QCLICK_ROOT_VIEWPORT_MARGIN = 8

QFAV_UI_ROOT_HEIGHT = 22
QFAV_UI_ROOT_MIN_WIDTH = 72
QFAV_UI_ROOT_MAX_WIDTH = 148

# Semantic order around the root wheel, starting at Right and rotating CCW.
# Empty sectors never remain dead zones: they are absorbed by occupied slots.
QCLICK_ROOT_SECTOR_ORDER = (1, 5, 3, 4, 0, 6, 2, 7)
QCLICK_ROOT_SECTOR_RING_INDEX = {
    slot_index: ring_index
    for ring_index, slot_index in enumerate(QCLICK_ROOT_SECTOR_ORDER)
}
QCLICK_ROOT_DIAGONAL_FALLBACKS = {
    4: (0, 3),  # left-up  -> left, then up
    5: (1, 3),  # right-up -> right, then up
    6: (0, 2),  # left-down -> left, then down
    7: (1, 2),  # right-down -> right, then down
}
QCLICK_ROOT_CARDINAL_SLOTS = {0, 1, 2, 3}
QCLICK_ROOT_SECTOR_HYSTERESIS_DEGREES = 4.0

_REGISTERED = False
_ACTIVE_WHEEL = None
_PENDING_WHEELS = set()
DOUBLE_TAP_INTERVAL = 0.32
# The hidden handoff swallows all input until the selecting release arrives.
# If that release is ever lost, give input back instead of looking frozen.
HANDOFF_RELEASE_TIMEOUT = 4.0
OPEN_DURATION = 0.12
ANIMATION_INTERVAL = 1.0 / 60.0


def _open_progress(elapsed):
    """Legacy 120 ms cubic ease-out, independent of event/frame frequency."""
    value = max(0.0, min(1.0, elapsed / OPEN_DURATION))
    return 1.0 - (1.0 - value) ** 3


def _open_position(center, target, progress):
    """Draw-only interpolation; final hit geometry never moves."""
    return tuple(a + (b - a) * progress for a, b in zip(center, target))


def _with_alpha(color, alpha):
    return (*color[:3], color[3] * alpha)


# ---------------------------------------------------------------------------
# Semantic sector model (ported 1.x pure helpers)
# ---------------------------------------------------------------------------


def _root_semantic_sector_slot(angle_radians):
    """Quantize a pointer angle to one of the eight semantic root sectors."""
    turn = (float(angle_radians) % (math.pi * 2.0)) / (math.pi * 2.0)
    ring_index = int(math.floor(turn * 8.0 + 0.5)) % 8
    return QCLICK_ROOT_SECTOR_ORDER[ring_index]


def _root_sector_ring_distance(first_slot, second_slot):
    first = QCLICK_ROOT_SECTOR_RING_INDEX[first_slot]
    second = QCLICK_ROOT_SECTOR_RING_INDEX[second_slot]
    direct = abs(first - second)
    return min(direct, 8 - direct)


def _root_sector_owner_map(slots):
    """Map every semantic root sector to an occupied root slot."""
    occupied = [
        slot_index
        for slot_index, entry in enumerate(slots[:8])
        if entry
    ]
    if not occupied:
        return {}

    occupied_set = set(occupied)
    owner_map = {}

    for semantic_slot in QCLICK_ROOT_SECTOR_ORDER:
        if semantic_slot in occupied_set:
            owner_map[semantic_slot] = semantic_slot
            continue

        # A missing diagonal belongs to the same-side horizontal cardinal first.
        fallback_owner = -1
        for candidate in QCLICK_ROOT_DIAGONAL_FALLBACKS.get(semantic_slot, ()):
            if candidate in occupied_set:
                fallback_owner = candidate
                break

        if fallback_owner < 0:
            ranked = sorted(
                occupied,
                key=lambda candidate: (
                    _root_sector_ring_distance(semantic_slot, candidate),
                    0 if candidate in QCLICK_ROOT_CARDINAL_SLOTS else 1,
                    QCLICK_ROOT_SECTOR_RING_INDEX[candidate],
                ),
            )
            fallback_owner = ranked[0]

        owner_map[semantic_slot] = fallback_owner

    return owner_map


def _root_sector_owner_for_angle(slots, angle_radians):
    semantic_slot = _root_semantic_sector_slot(angle_radians)
    return _root_sector_owner_map(slots).get(semantic_slot, -1)


def _root_sector_owner_with_hysteresis(slots, angle_radians, previous_owner=-1):
    """Delay owner switches slightly while crossing root-sector boundaries."""
    candidate = _root_sector_owner_for_angle(slots, angle_radians)
    if candidate < 0:
        return -1

    if (
        previous_owner < 0
        or previous_owner == candidate
        or previous_owner >= len(slots)
        or not slots[previous_owner]
    ):
        return candidate

    margin = math.radians(QCLICK_ROOT_SECTOR_HYSTERESIS_DEGREES)
    owner_before = _root_sector_owner_for_angle(slots, angle_radians - margin)
    owner_after = _root_sector_owner_for_angle(slots, angle_radians + margin)

    if owner_before == candidate and owner_after == candidate:
        return candidate
    return previous_owner


def _root_sector_hit(mouse, center, slots, previous_owner):
    mx, my = mouse
    cx, cy = center
    dx = mx - cx
    dy = my - cy
    if math.hypot(dx, dy) <= QFAV_DIRECTION_DEADZONE:
        return -1
    return _root_sector_owner_with_hysteresis(
        slots,
        math.atan2(dy, dx),
        previous_owner=previous_owner,
    )


# ---------------------------------------------------------------------------
# Card layout (ported 1.x pure helpers)
# ---------------------------------------------------------------------------


def _wheel_draw_size(size=11):
    try:
        ui_scale = float(getattr(bpy.context.preferences.system, "ui_scale", 1.0))
    except Exception:
        ui_scale = 1.0
    return max(9, int(float(size) * ui_scale))


@lru_cache(maxsize=2048)
def _cached_text_dimensions(text, draw_size):
    font_id = 0
    blf.size(font_id, int(draw_size))
    return tuple(float(value) for value in blf.dimensions(font_id, str(text or "")))


def _card_size(label):
    width, _height = _cached_text_dimensions(str(label or ""), _wheel_draw_size(11))
    return (
        max(QFAV_UI_ROOT_MIN_WIDTH, min(QFAV_UI_ROOT_MAX_WIDTH, width + 18)),
        QFAV_UI_ROOT_HEIGHT,
    )


def _entry_label(entry):
    if isinstance(entry, dict):
        label = str(entry.get("_display_label", entry.get("label", "")) or T("Action"))
        prefix = {"FOLDER": "> ", "GROUP": "+ "}.get(entry.get("kind"), "")
        return prefix + label
    return str(entry or "")


def _card_rect(x, y, label, width_limit=None):
    width, height = _card_size(label)
    if width_limit is not None:
        width = min(width, width_limit)
    return (x - width * 0.5, y - height * 0.5, x + width * 0.5, y + height * 0.5)


def _rect_overlap_area(first, second, gap=0.0):
    left = max(first[0] - gap * 0.5, second[0] - gap * 0.5)
    bottom = max(first[1] - gap * 0.5, second[1] - gap * 0.5)
    right = min(first[2] + gap * 0.5, second[2] + gap * 0.5)
    top = min(first[3] + gap * 0.5, second[3] + gap * 0.5)
    if right <= left or top <= bottom:
        return 0.0
    return (right - left) * (top - bottom)


def _viewport_overflow(rect, region_size, margin=QCLICK_ROOT_VIEWPORT_MARGIN):
    width, height = region_size
    if width <= 0 or height <= 0:
        return 0.0
    overflow = 0.0
    overflow += max(0.0, margin - rect[0])
    overflow += max(0.0, margin - rect[1])
    overflow += max(0.0, rect[2] - (width - margin))
    overflow += max(0.0, rect[3] - (height - margin))
    return overflow


def _fixed_root_position_at(center, slot_index, outward=0.0):
    cx, cy = center
    if slot_index == 0:
        return (cx - QCLICK_ROOT_CARDINAL_X, cy)
    if slot_index == 1:
        return (cx + QCLICK_ROOT_CARDINAL_X, cy)
    if slot_index == 2:
        return (cx, cy - QCLICK_ROOT_CARDINAL_Y)
    if slot_index == 3:
        return (cx, cy + QCLICK_ROOT_CARDINAL_Y)

    base_vectors = {
        4: (-QCLICK_ROOT_DIAGONAL_X, QCLICK_ROOT_DIAGONAL_Y),
        5: (QCLICK_ROOT_DIAGONAL_X, QCLICK_ROOT_DIAGONAL_Y),
        6: (-QCLICK_ROOT_DIAGONAL_X, -QCLICK_ROOT_DIAGONAL_Y),
        7: (QCLICK_ROOT_DIAGONAL_X, -QCLICK_ROOT_DIAGONAL_Y),
    }
    dx, dy = base_vectors.get(slot_index, (0.0, 0.0))
    length = math.hypot(dx, dy) or 1.0
    nx, ny = dx / length, dy / length
    return (cx + dx + nx * outward, cy + dy + ny * outward)


def _root_candidate_penalty(candidate_rect, resolved_rectangles, region_size):
    overlap_area = 0.0
    collision_count = 0
    for existing_rect in resolved_rectangles:
        area = _rect_overlap_area(candidate_rect, existing_rect, QCLICK_ROOT_CARD_GAP)
        if area > 0.0:
            collision_count += 1
            overlap_area += area
    return (
        collision_count * 1_000_000.0
        + overlap_area * 10.0
        + _viewport_overflow(candidate_rect, region_size)
    )


def _resolve_root_positions(slots, center, region_size):
    """Resolve native-like first-layer anchors with adaptive diagonals."""
    positions = {}
    resolved_rectangles = []

    for slot_index in (0, 1, 2, 3):
        if slot_index >= len(slots) or not slots[slot_index]:
            continue
        x, y = _fixed_root_position_at(center, slot_index)
        positions[slot_index] = (x, y)
        resolved_rectangles.append(_card_rect(x, y, _entry_label(slots[slot_index])))

    for slot_index in (4, 5, 6, 7):
        if slot_index >= len(slots) or not slots[slot_index]:
            continue

        label = _entry_label(slots[slot_index])
        best_position = _fixed_root_position_at(center, slot_index)
        best_rect = _card_rect(best_position[0], best_position[1], label)
        best_penalty = _root_candidate_penalty(best_rect, resolved_rectangles, region_size)

        outward = 0.0
        while outward <= QCLICK_ROOT_DIAGONAL_OUTWARD_MAX:
            x, y = _fixed_root_position_at(center, slot_index, outward)
            candidate_rect = _card_rect(x, y, label)
            penalty = _root_candidate_penalty(candidate_rect, resolved_rectangles, region_size)
            if penalty < best_penalty:
                best_penalty = penalty
                best_position = (x, y)
                best_rect = candidate_rect
            if penalty <= 0.0:
                best_position = (x, y)
                best_rect = candidate_rect
                break
            outward += QCLICK_ROOT_DIAGONAL_OUTWARD_STEP

        positions[slot_index] = best_position
        resolved_rectangles.append(best_rect)

    return positions


# ---------------------------------------------------------------------------
# Viewport-safe presentation. Gesture origins remain independent.
# ---------------------------------------------------------------------------


def _visible_bounds(context):
    region = context.region
    left, bottom, right, top = 0.0, 0.0, float(region.width), float(region.height)
    for other in context.area.regions:
        if other.type in {"HEADER", "TOOL_HEADER", "ASSET_SHELF", "ASSET_SHELF_HEADER"} and other.height > 1:
            y0, y1 = other.y - region.y, other.y + other.height - region.y
            if y0 <= bottom < y1 < top:
                bottom = float(y1)
            elif bottom < y0 < top <= y1:
                top = float(y0)
            continue
        if other.type not in {"UI", "TOOLS"} or other.width <= 1:
            continue
        if other.type == "UI" and not context.space_data.show_region_ui:
            continue
        if other.type == "TOOLS" and not context.space_data.show_region_toolbar:
            continue
        x0, x1 = other.x - region.x, other.x + other.width - region.x
        if x0 <= left < x1 < right:
            left = float(x1)
        elif left < x0 < right <= x1:
            right = float(x0)
    return (left + 8, bottom + 8, right - 8, top - 8)


def _root_presentation(slots, center, bounds):
    """Keep gesture geometry independent of viewport edges and native panels."""
    positions = _resolve_root_positions(slots, center, (0, 0))
    return center, positions, None


# GPU drawing


def _draw_circle_fill(x, y, radius, color, segments=48):
    coords = []
    for index in range(segments):
        first = math.tau * index / segments
        second = math.tau * (index + 1) / segments
        coords.extend(
            (
                (x, y),
                (x + math.cos(first) * radius, y + math.sin(first) * radius),
                (x + math.cos(second) * radius, y + math.sin(second) * radius),
            )
        )
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRIS", {"pos": coords}).draw(shader)


def _draw_donut_fill(x, y, inner_radius, outer_radius, color, segments=48):
    coords = []
    for index in range(segments):
        first = math.tau * index / segments
        second = math.tau * (index + 1) / segments
        outer_a = (x + math.cos(first) * outer_radius, y + math.sin(first) * outer_radius)
        outer_b = (x + math.cos(second) * outer_radius, y + math.sin(second) * outer_radius)
        inner_a = (x + math.cos(first) * inner_radius, y + math.sin(first) * inner_radius)
        inner_b = (x + math.cos(second) * inner_radius, y + math.sin(second) * inner_radius)
        coords.extend((inner_a, outer_a, outer_b, inner_a, outer_b, inner_b))
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRIS", {"pos": coords}).draw(shader)


def _direction_arc_vertices(center, mouse):
    dx, dy = mouse[0] - center[0], mouse[1] - center[1]
    if math.hypot(dx, dy) <= QFAV_CENTER_RADIUS:
        return ()
    angle = math.atan2(dy, dx)
    coords = []
    for index in range(12):
        a = angle - 0.34 + 0.68 * index / 12
        b = angle - 0.34 + 0.68 * (index + 1) / 12
        def point(theta, radius):
            return (center[0] + math.cos(theta) * radius, center[1] + math.sin(theta) * radius)
        ia, ib = point(a, QCLICK_CENTER_RING_INNER_RADIUS), point(b, QCLICK_CENTER_RING_INNER_RADIUS)
        oa, ob = point(a, QFAV_CENTER_RADIUS), point(b, QFAV_CENTER_RADIUS)
        coords.extend((ia, oa, ob, ia, ob, ib))
    return tuple(coords)


def _draw_direction_arc(center, mouse, color, size_factor=1.0):
    vertices = _direction_arc_vertices(center, mouse)
    vertices = tuple((center[0] + (x - center[0]) * size_factor,
                      center[1] + (y - center[1]) * size_factor) for x, y in vertices)
    if not vertices:
        return
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRIS", {"pos": vertices}).draw(shader)


def _route_vertices(start, end, progress=1.0):
    distance = math.dist(start, end)
    if distance <= 2 * QFAV_CENTER_RADIUS:
        return ()
    ux, uy = (end[0] - start[0]) / distance, (end[1] - start[1]) / distance
    a = (start[0] + ux * QFAV_CENTER_RADIUS, start[1] + uy * QFAV_CENTER_RADIUS)
    length = (distance - 2 * QFAV_CENTER_RADIUS) * max(0, min(1, progress))
    b = (a[0] + ux * length, a[1] + uy * length)
    nx, ny = -uy * 0.75, ux * 0.75
    return ((a[0]+nx, a[1]+ny), (a[0]-nx, a[1]-ny), (b[0]-nx, b[1]-ny),
            (a[0]+nx, a[1]+ny), (b[0]-nx, b[1]-ny), (b[0]+nx, b[1]+ny))


def _draw_route(start, end, color, progress):
    vertices = _route_vertices(start, end, progress)
    if not vertices:
        return
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRIS", {"pos": vertices}).draw(shader)


def _wheel_card_colors(palette, previous, hovered):
    if previous:
        return palette["inner_sel"], palette["item"] if hovered else palette["inner_sel"], palette["text_sel"]
    return (palette["item"] if hovered else palette["inner"],
            palette["outline"], palette["text_sel"] if hovered else palette["text"])


def _draw_return_marker(x, y, palette, hovered=False, size_factor=1.0, label=None):
    """Allow a smaller group marker while preserving navigation hit targets."""
    inner_radius = QCLICK_CENTER_RING_INNER_RADIUS * size_factor
    outer_radius = QFAV_CENTER_RADIUS * size_factor
    _draw_donut_fill(x, y, inner_radius, outer_radius, palette["item"] if hovered else palette["inner"])
    _draw_ring(x, y, outer_radius, palette["outline"])
    _draw_ring(x, y, inner_radius, palette["outline"])
    if label:
        _draw_wheel_text(label, x, y, 12, palette["text"])
    else:
        _draw_circle_fill(x, y, 2.5, palette["text_sel"] if hovered else palette["text"])


QCLICK_GROUP_GUARD_RADIUS = 16


def _pointer_guard_rect(point):
    """Suppress selection only around the pointer that opened the group.

Guarding the whole source card meant a wide card swallowed most of the travel
toward the new items: nothing highlighted until the pointer had crossed half
the card's width, which on a long label is nearly the whole distance to the
next item.  A small box around the entry pointer releases after a short move
in any direction, so highlighting resumes at the same distance as elsewhere.
"""
    x, y = point
    return (x - QCLICK_GROUP_GUARD_RADIUS, y - QCLICK_GROUP_GUARD_RADIUS,
            x + QCLICK_GROUP_GUARD_RADIUS, y + QCLICK_GROUP_GUARD_RADIUS)


def _draw_ring(x, y, radius, color, width=1.0, segments=48):
    points = tuple(
        (x + math.cos(math.tau * index / segments) * radius, y + math.sin(math.tau * index / segments) * radius)
        for index in range(segments)
    )
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
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


_TEXT_DESCENT_RATIO = 0.2


def _draw_wheel_text(text, x, y, size, color, max_width=None):
    font_id = 0
    draw_size = _wheel_draw_size(size)
    blf.size(font_id, draw_size)
    text = str(text or "")
    if max_width is not None:
        text = _fit_text_px(font_id, text, float(max_width))
    if not text:
        return
    # The themed card already supplies contrast, like native pie labels.
    try:
        blf.disable(font_id, blf.SHADOW)
    except Exception:
        pass
    width, height = blf.dimensions(font_id, text)
    # Same descent bias as the sidebar text: centring on blf's reported box
    # alone lands the glyphs low inside a card.
    blf.position(font_id, x - width * 0.5,
                 y - height * 0.5 + draw_size * _TEXT_DESCENT_RATIO, 0)
    blf.color(font_id, *color)
    blf.draw(font_id, text)
    try:
        blf.disable(font_id, blf.SHADOW)
    except Exception:
        pass
    # BLF may alter GPU state. Restore alpha compositing for later cards.
    try:
        gpu.state.blend_set("ALPHA")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Modal wheel operator
# ---------------------------------------------------------------------------


class QCLICK2_OT_call_wheel(Operator):
    bl_idname = "qclick2.call_wheel"
    bl_label = "Open Q&Click Wheel"

    start_layer: StringProperty(default=persistence.ROOT, options={"HIDDEN", "SKIP_SAVE"})

    _draw_handle = None
    _draw_space = None
    _area_pointer = 0
    _center = (0, 0)
    _mouse = (0, 0)
    _slots = ()
    _positions = None
    _hover_slot = -1
    _root_sector_slot = -1
    _theme = None
    _trigger_event_type = "Q"
    _pending_release_action = None
    _pending_release_event_type = ""
    _pending_handoff_started_at = 0.0
    _pending_handoff_delay = 0.0
    _pending_began_at = 0.0
    _action_handoff_timer = None
    _previous_action = None
    _previous_slots = ()
    _context_key = ""
    _center_repeat_armed = True
    _trigger_held = False
    _q_release_time = None
    _pending_trigger_release = False
    _profile = None
    _layer_path = ()
    _group_id = ""
    _group_anchor = 1
    _group_parent = None
    _group_guard = None
    _group_sizes = None
    _group_reenter_block = None
    _animation_timer = None
    _animation_wm = None
    _animation_window = None
    _open_started_at = None
    _visual_center = (0, 0)
    _bounds = (0, 0, 0, 0)
    _card_width_limit = None
    _tooltip = None

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None
            and context.area.type in persistence.SUPPORTED_AREAS
            and context.region is not None
            and context.region.type == "WINDOW"
        )

    def execute(self, context):
        # Fallback path: open the native pie over the same slots.
        try:
            bpy.ops.wm.call_menu_pie(name="QCLICK2_MT_main_pie")
            return {"FINISHED"}
        except Exception:
            return {"CANCELLED"}

    def invoke(self, context, event):
        global _ACTIVE_WHEEL
        if not self.poll(context):
            return {"PASS_THROUGH"}
        if getattr(event, "is_repeat", False):
            return {"CANCELLED"}

        previous = _ACTIVE_WHEEL
        if previous is not None and previous is not self:
            try:
                previous._finish(context)
            except Exception:
                pass
        _ACTIVE_WHEEL = self
        self._cancelled = False

        self._animation_wm = context.window_manager
        self._animation_window = context.window
        self._area_pointer = int(context.area.as_pointer())
        self._center = (event.mouse_region_x, event.mouse_region_y)
        self._bounds = _visible_bounds(context)
        self._tooltip = _HoverTip()
        self._mouse = self._center
        self._hover_slot = -1
        self._root_sector_slot = -1
        self._pending_release_action = None
        self._pending_is_repeat = False
        self._pending_release_event_type = ""
        self._action_handoff_timer = None
        from_folder_button = self.start_layer != persistence.ROOT
        self._trigger_event_type = "Q" if from_folder_button else str(event.type or "Q")
        self._trigger_held = not from_folder_button
        self._q_release_time = None
        self._center_repeat_armed = not from_folder_button
        self._pending_trigger_release = False
        self._context_key = _repeat_context_key(context)
        self._previous_action = _last_action_for_context(context)
        self._theme = _native_theme()
        # Snapshot once: MOUSEMOVE and drawing never parse JSON or touch disk.
        self._profile = persistence.load_profile(context)
        self._previous_action = persistence.presentation_entry(self._previous_action, self._profile)
        self._layer_path = []
        self._group_id = ""
        self._group_parent = None
        self._group_guard = None
        self._group_reenter_block = None
        if from_folder_button:
            if self.start_layer not in self._profile["folders"] and self.start_layer not in self._profile["groups"]:
                self._finish(context)
                self.report({"WARNING"}, T("The sub-wheel or group no longer exists"))
                return {"CANCELLED"}
            if self.start_layer in self._profile["folders"]:
                self._layer_path.append(self.start_layer)
        try:
            self._refresh_layer()
            if from_folder_button and self.start_layer in self._profile["groups"]:
                self._enter_group(self.start_layer, 1, center=self._mouse)
        except ValueError as exc:
            self._finish(context)
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}

        self._draw_space = space_class(context.area.type)
        self._draw_handle = self._draw_space.draw_handler_add(
            self._draw_callback, (), "WINDOW", "POST_PIXEL"
        )
        context.window_manager.modal_handler_add(self)
        self._start_open_animation()
        self._tag_redraw()
        return {"RUNNING_MODAL"}

    # -- event handling ------------------------------------------------------

    def modal(self, context, event):
        try:
            if not _REGISTERED or getattr(self, "_cancelled", False):
                self._cleanup_action_handoff(context)
                self._finish(context)
                return {"CANCELLED"}
            if _repeat_context_key(context) != self._context_key:
                self._cleanup_action_handoff(context)
                self._finish(context)
                return {"CANCELLED"}

            # A resize changes both clipping and hit geometry. Cancel rather than
            # execute against the invocation's stale layout.
            if getattr(context, "region", None) is not None and context.region.type == "WINDOW" and _visible_bounds(context) != self._bounds:
                self._cleanup_action_handoff(context)
                self._finish(context)
                return {"CANCELLED"}

            # Universal source-release handoff: the overlay is already hidden,
            # but this modal stays alive until the selecting key or mouse
            # release and the settling delay have been consumed, so the release
            # event can never leak into UI opened by the chosen action.
            if self._pending_release_action is not None:
                if self._is_cancel_event(event):
                    self._cleanup_action_handoff(context)
                    self._finish(context)
                    return {"CANCELLED"}

                if self._pending_release_event_type or self._pending_trigger_release:
                    if time.monotonic() - self._pending_began_at > HANDOFF_RELEASE_TIMEOUT:
                        self._cleanup_action_handoff(context)
                        self._finish(context)
                        self.report({"WARNING"}, T("No key release was detected; the action was cancelled"))
                        return {"CANCELLED"}
                    if event.type == self._trigger_event_type and event.value == "RELEASE":
                        self._pending_trigger_release = False
                    if (
                        event.type == self._pending_release_event_type
                        and event.value == "RELEASE"
                    ):
                        self._pending_release_event_type = ""
                    if not self._pending_release_event_type and not self._pending_trigger_release:
                        self._pending_handoff_started_at = time.monotonic()
                    return {"RUNNING_MODAL"}

                if event.type == "TIMER":
                    elapsed = time.monotonic() - self._pending_handoff_started_at
                    if elapsed < self._pending_handoff_delay:
                        return {"RUNNING_MODAL"}
                    action = self._pending_release_action
                    is_repeat = self._pending_is_repeat
                    self._cleanup_action_handoff(context)
                    self._finish(context)
                    try:
                        _schedule_action_entry(context, action, source_release_settled=True, repeat=is_repeat)
                    except Exception as exc:
                        self.report({"ERROR"}, str(exc))
                        return {"CANCELLED"}
                    return {"FINISHED"}

                # Swallow every leftover event while ownership is transferred.
                return {"RUNNING_MODAL"}

            if self._is_cancel_event(event):
                self._finish(context)
                return {"CANCELLED"}

            # Handoff TIMER handling above must retain priority. Repaint only
            # during opening, and remove the timer after the final settled frame.
            if event.type == "TIMER":
                # Blender Event exposes no timer identity. Any TIMER can advance
                # this wall-clock animation while our own repaint timer exists.
                if self._animation_timer is not None:
                    if self._open_started_at is not None and time.monotonic() - self._open_started_at >= OPEN_DURATION:
                        self._stop_open_animation()
                    self._tag_redraw()
                return {"RUNNING_MODAL"}

            if event.type == "MOUSEMOVE":
                self._mouse = (event.mouse_region_x, event.mouse_region_y)
                if not self._is_center_zone():
                    self._center_repeat_armed = False
                self._update_hover()
                self._tag_redraw()
                return {"RUNNING_MODAL"}

            if event.type == self._trigger_event_type and event.value == "PRESS":
                if self._trigger_held or getattr(event, "is_repeat", False):
                    return {"RUNNING_MODAL"}
                self._trigger_held = True
                if self._center_repeat_armed and self._is_center_zone() and self._q_release_time is not None:
                    if time.monotonic() - self._q_release_time <= DOUBLE_TAP_INTERVAL:
                        return self._repeat_previous(context, self._trigger_event_type)
                return {"RUNNING_MODAL"}

            if event.type == self._trigger_event_type and event.value == "RELEASE":
                self._trigger_held = False
                if self._is_center_zone() or (self._group_guard and groups.contains(self._mouse, self._group_guard, 8)):
                    # A tap keeps the wheel open for mouse selection.
                    if self._center_repeat_armed:
                        self._q_release_time = time.monotonic()
                    self._tag_redraw()
                    return {"RUNNING_MODAL"}
                return self._activate_hovered(context, release_event_type="")

            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                direct = self._card_at(self._mouse)
                if direct >= 0:
                    self._hover_slot = direct
                    return self._activate_hovered(context, release_event_type="LEFTMOUSE")
                if math.dist(self._mouse, self._visual_center) <= QFAV_DIRECTION_DEADZONE:
                    if self._group_id:
                        return self._leave_group()
                    if self._layer_path:
                        self._layer_path.pop()
                        return self._navigate_layer()
                    if self._center_repeat_armed:
                        return self._repeat_previous(context, "LEFTMOUSE")
                    return {"RUNNING_MODAL"}
                return self._activate_hovered(context, release_event_type="LEFTMOUSE")

            if event.type in {"RET", "NUMPAD_ENTER"} and event.value == "PRESS":
                return self._activate_hovered(context, release_event_type=event.type)

            return {"RUNNING_MODAL"}
        except Exception as exc:
            self._cleanup_action_handoff(context)
            self._finish(context)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def _is_cancel_event(self, event):
        if event.type == "WINDOW_DEACTIVATE":
            return True
        if str(getattr(event, "value", "") or "") != "PRESS":
            return False
        event_type = str(getattr(event, "type", "") or "")
        if event_type == "ESC":
            return True
        return event_type == "RIGHTMOUSE" and self._trigger_event_type != "RIGHTMOUSE"

    def _is_center_zone(self):
        return (
            math.hypot(self._mouse[0] - self._center[0], self._mouse[1] - self._center[1])
            <= QFAV_DIRECTION_DEADZONE
        )

    def _hit_slot(self):
        if self._group_id:
            if self._group_guard and groups.contains(self._mouse, self._group_guard, 8):
                return -1
            self._group_guard = None
            return groups.hit(self._mouse, self._visual_center, self._positions, self._group_sizes)
        if not self._trigger_held and (self._visual_center != self._center or self._card_width_limit is not None):
            # After tapping Q, visible cards own point selection. Do not map a
            # translated left card to the original origin's right-hand sector.
            return self._card_at(self._mouse)
        owner = _root_sector_hit(self._mouse, self._center, list(self._slots), self._root_sector_slot)
        self._root_sector_slot = owner
        return owner

    def _slot_rect(self, index, position=None):
        return _card_rect(*(position or self._positions[index]), _entry_label(self._slots[index]), self._card_width_limit)

    def _card_at(self, point):
        for index in self._positions or {}:
            if groups.contains(point, self._slot_rect(index)):
                return index
        return -1

    def _update_hover(self):
        if self._group_id and self._group_parent:
            previous_center = self._group_parent["visual_center"]
            if previous_center != self._center and math.dist(self._mouse, previous_center) <= 24:
                self._leave_group()
                return
        self._hover_slot = self._hit_slot()
        if self._group_reenter_block:
            if groups.contains(self._mouse, self._group_reenter_block, 8):
                return
            self._group_reenter_block = None
        if self._hover_slot >= 0:
            entry = self._slots[self._hover_slot]
            if entry and entry.get("kind") == "GROUP":
                rect = self._slot_rect(self._hover_slot)
                if groups.contains(self._mouse, rect, 8):
                    self._enter_group(entry["layer_id"], self._hover_slot)

    def _enter_group(self, group_id, anchor, center=None):
        ancestors = {self._group_id}
        previous = self._group_parent
        while previous:
            ancestors.add(previous.get("group_id", ""))
            previous = previous.get("parent")
        if group_id not in self._profile["groups"] or group_id in ancestors:
            return {"RUNNING_MODAL"}
        self._group_parent = {"center": self._center, "visual_center": self._visual_center,
                              "width_limit": self._card_width_limit, "slots": self._slots, "positions": self._positions,
                              "group_id": self._group_id, "parent": self._group_parent,
                              "anchor": self._group_anchor}
        position = center if center is not None else self._positions.get(anchor, self._mouse)
        entry = self._slots[anchor] if center is None and anchor < len(self._slots) else None
        self._group_guard = _pointer_guard_rect(self._mouse) if entry else None
        self._center = position
        self._group_id = group_id
        self._group_anchor = anchor
        self._group_reenter_block = None
        self._reset_navigation()
        return {"RUNNING_MODAL"}

    def _leave_group(self):
        # A center-click return leaves the pointer on the original group card.
        # Require an actual exit before its hover trigger can fire again.
        parent = self._group_parent
        if parent is None:
            return {"RUNNING_MODAL"}
        entry = parent["slots"][self._group_anchor]
        position = parent["positions"].get(self._group_anchor)
        rect = _card_rect(*position, _entry_label(entry), parent["width_limit"]) if position and entry and entry.get("kind") == "GROUP" else None
        self._group_reenter_block = rect if rect and groups.contains(self._mouse, rect, 8) else None
        self._center = parent["center"]
        self._group_id = parent["group_id"]
        self._group_parent = parent["parent"]
        self._group_anchor = parent["anchor"]
        self._group_guard = None
        self._reset_navigation(animate=False)
        return {"RUNNING_MODAL"}

    def _reset_navigation(self, animate=True):
        self._stop_open_animation()
        self._tooltip.reset()
        self._hover_slot = -1
        self._root_sector_slot = -1
        self._q_release_time = None
        self._center_repeat_armed = False
        self._refresh_layer()
        if animate:
            self._start_open_animation()
        self._tag_redraw()

    def _repeat_previous(self, context, release_event_type):
        self._previous_action = _last_action_for_context(context)
        if self._previous_action is None:
            self.report({"INFO"}, T("No last action in this mode yet; run a tool from the wheel first"))
            self._tag_redraw()
            return {"RUNNING_MODAL"}
        return self._begin_action_handoff(context, self._previous_action, release_event_type, repeat=True)

    def _activate_hovered(self, context, release_event_type=""):
        slot = self._hover_slot
        entry = self._slots[slot] if 0 <= slot < len(self._slots) else None
        if isinstance(entry, dict) and entry.get("kind") == "GROUP":
            return self._enter_group(entry["layer_id"], slot)
        if isinstance(entry, dict) and entry.get("kind") == "FOLDER":
            folder_id = entry.get("layer_id")
            if folder_id in self._profile["folders"] and folder_id not in self._layer_path:
                self._layer_path.append(folder_id)
                return self._navigate_layer()
            self.report({"WARNING"}, T("Cannot open this sub-wheel"))
            return {"RUNNING_MODAL"}
        if not isinstance(entry, dict) or entry.get("kind") != "ACTION":
            if self._group_id:
                return {"RUNNING_MODAL"}
            self._finish(context)
            return {"CANCELLED"}
        return self._begin_action_handoff(context, entry, release_event_type)

    def _refresh_layer(self):
        layer_id = self._group_id or (self._layer_path[-1] if self._layer_path else persistence.ROOT)
        self._slots = tuple(persistence.presentation_entry(entry, self._profile) for entry in persistence.profile_slots(self._profile, layer_id))
        identity = _action_identity(self._previous_action) if self._previous_action else None
        self._previous_slots = tuple(
            bool(entry and entry.get("kind") == "ACTION" and identity is not None
                 and _action_identity(entry) == identity) for entry in self._slots
        )
        if self._group_id:
            self._group_sizes = {index: _card_size(_entry_label(entry)) for index, entry in enumerate(self._slots) if entry}
            direction = self._group_anchor
            if self._group_parent and self._group_parent.get("group_id"):
                px, py = self._group_parent["visual_center"]
                dx, dy = self._center[0] - px, self._center[1] - py
                direction = max(range(8), key=lambda i: groups.DIRECTIONS[i][0] * dx + groups.DIRECTIONS[i][1] * dy)
            self._positions = groups.resolve_positions(self._group_sizes, self._center,
                                                       direction, None)
            self._visual_center = self._center
            self._card_width_limit = None
        else:
            self._visual_center, self._positions, self._card_width_limit = _root_presentation(self._slots, self._center, self._bounds)

    def _navigate_layer(self):
        # Recenter the drawing only, never move the system pointer. Navigation
        # is not an action and must not arm repeat or replace action history.
        self._center = self._mouse
        self._group_reenter_block = None
        self._reset_navigation()
        return {"RUNNING_MODAL"}

    def _begin_action_handoff(self, context, action, release_event_type="", repeat=False):
        action = _canonicalize_action(copy.deepcopy(action))
        self._pending_release_action = action
        self._pending_began_at = time.monotonic()
        self._pending_is_repeat = bool(repeat)
        self._pending_release_event_type = str(release_event_type or "")
        self._pending_trigger_release = bool(self._trigger_held and release_event_type != self._trigger_event_type)
        self._pending_handoff_started_at = (
            0.0 if self._pending_release_event_type else time.monotonic()
        )
        self._pending_handoff_delay = (
            QFAV_NATIVE_UI_HANDOFF_DELAY
            if _action_needs_pointer_release_barrier(action)
            else QFAV_GENERAL_ACTION_HANDOFF_DELAY
        )

        # Hide the overlay immediately, but keep this operator modal until it
        # has swallowed the source event and transferred ownership safely.
        self._finish(context)
        try:
            self._ensure_action_handoff_timer(context)
        except Exception:
            # Conservative fallback for unusual contexts without a window timer.
            pending = self._pending_release_action
            is_repeat = self._pending_is_repeat
            self._cleanup_action_handoff(context)
            try:
                _schedule_action_entry(context, pending, repeat=is_repeat)
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def _ensure_action_handoff_timer(self, context):
        if self._action_handoff_timer is None:
            self._action_handoff_timer = context.window_manager.event_timer_add(
                QFAV_ACTION_HANDOFF_TIMER_INTERVAL,
                window=context.window,
            )
        _PENDING_WHEELS.add(self)

    def _cleanup_action_handoff(self, context):
        _PENDING_WHEELS.discard(self)
        if self._action_handoff_timer is not None:
            try:
                context.window_manager.event_timer_remove(self._action_handoff_timer)
            except Exception:
                pass
            self._action_handoff_timer = None
        self._pending_release_action = None
        self._pending_release_event_type = ""
        self._pending_trigger_release = False

    def _start_open_animation(self):
        self._stop_open_animation()
        try:
            self._animation_timer = self._animation_wm.event_timer_add(
                ANIMATION_INTERVAL, window=self._animation_window,
            )
        except Exception:
            # Drawing/selection still work in contexts without a window timer.
            self._animation_timer = None

    def _stop_open_animation(self):
        if self._animation_timer is not None:
            try:
                self._animation_wm.event_timer_remove(self._animation_timer)
            except Exception:
                pass
        self._animation_timer = None
        self._open_started_at = None

    def _draw_open_progress(self):
        if self._animation_timer is None:
            return 1.0
        now = time.monotonic()
        if self._open_started_at is None:
            # Start with the first visible frame, not during disk/layout setup.
            self._open_started_at = now
        return _open_progress(now - self._open_started_at)

    def _finish(self, context):
        global _ACTIVE_WHEEL
        self._stop_open_animation()
        if self._tooltip is not None:
            self._tooltip.reset()
        if self._draw_handle is not None:
            try:
                (self._draw_space or bpy.types.SpaceView3D).draw_handler_remove(self._draw_handle, "WINDOW")
            except Exception:
                pass
            self._draw_handle = None
        if _ACTIVE_WHEEL is self:
            _ACTIVE_WHEEL = None
        self._tag_redraw()

    def cancel(self, context):
        self._cleanup_action_handoff(context)
        self._finish(context)

    def _tag_redraw(self):
        # Resolve by pointer: a stored Area object can outlive the area itself.
        try:
            redraw_area(self._area_pointer, ("WINDOW",))
        except Exception:
            pass

    # -- drawing ---------------------------------------------------------------

    def _draw_callback(self):
        context = bpy.context
        if context.area is None or int(context.area.as_pointer()) != self._area_pointer:
            return
        theme = self._theme or _native_theme()
        progress = self._draw_open_progress()
        card_alpha = 0.25 + 0.75 * progress
        wcol_pie = theme["wcol"]["pie"]
        cx, cy = self._visual_center
        can_return = bool(self._layer_path or self._group_id)
        repeat_available = bool(not can_return and self._previous_action and self._center_repeat_armed)
        center_hovered = (can_return or repeat_available) and math.dist(self._mouse, self._visual_center) <= QFAV_DIRECTION_DEADZONE
        try:
            gpu.state.blend_set("ALPHA")

            if self._group_id and self._group_parent:
                parent = self._group_parent
                # Ancestor actions stay hidden; keep navigation markers and routes.
                px, py = parent["visual_center"]
                if math.dist(parent["visual_center"], self._visual_center) > 34:
                    _draw_route(parent["visual_center"], self._visual_center, wcol_pie["outline"], progress)
                    _draw_return_marker(px, py, wcol_pie,
                                        size_factor=0.7 if parent["group_id"] else 1.0,
                                        label=None if parent["group_id"] else "QC")
                root_parent = parent.get("parent")
                while root_parent:
                    root_center = root_parent["visual_center"]
                    _draw_route(root_center, parent["visual_center"], wcol_pie["outline"], progress)
                    _draw_return_marker(*root_center, wcol_pie,
                                        size_factor=0.7 if root_parent["group_id"] else 1.0,
                                        label=None if root_parent["group_id"] else "QC")
                    parent = root_parent
                    root_parent = root_parent.get("parent")

            # Center ring.
            if can_return:
                _draw_return_marker(cx, cy, wcol_pie, center_hovered, size_factor=0.7 if self._group_id else 1.0)
            else:
                self._draw_main_center(cx, cy, theme, wcol_pie, center_hovered, repeat_available)
            if can_return and not self._group_id:
                hint = self._profile["folders"][self._layer_path[-1]]["label"]
                _draw_wheel_text(hint, cx, cy - 38, 12, _with_alpha(wcol_pie["text"], 0.88), max_width=170)
            if not (repeat_available and self._previous_action):
                # The repeat card already occupies the centre; an arc drawn
                # inside it would only cut across the label.
                _draw_direction_arc(self._visual_center, self._mouse, wcol_pie["inner_sel"],
                                    size_factor=0.7 if self._group_id else 1.0)

            self._draw_cards_and_tooltip(theme, progress, card_alpha)
        finally:
            try:
                gpu.state.blend_set("NONE")
            except Exception:
                pass

    def _draw_main_center(self, cx, cy, theme, wcol_pie, center_hovered, repeat_available):
            if repeat_available and self._previous_action:
                # A recorded repeat action takes the place of the centre ring,
                # so the wheel shows what a centre click will run.
                label = _entry_label(self._previous_action)
                rect = _card_rect(cx, cy, label)
                fill_color, border_color, text_color = _wheel_card_colors(
                    wcol_pie, True, center_hovered)
                _draw_widget(
                    rect,
                    wcol_pie,
                    theme,
                    state="hover" if center_hovered else "normal",
                    role="pie",
                    fill=fill_color,
                    border=border_color,
                )
                _draw_wheel_text(label, cx, cy, 11, text_color,
                                 max_width=rect[2] - rect[0] - 10.0)
                return
            _draw_donut_fill(
                cx,
                cy,
                QCLICK_CENTER_RING_INNER_RADIUS,
                QFAV_CENTER_RADIUS,
                wcol_pie["item"] if center_hovered else wcol_pie["inner"],
            )
            _draw_ring(cx, cy, QFAV_CENTER_RADIUS, wcol_pie["outline"])
            _draw_ring(cx, cy, QCLICK_CENTER_RING_INNER_RADIUS, wcol_pie["outline"])
            _draw_wheel_text("QC", cx, cy, 12, wcol_pie["text"])

    def _draw_cards_and_tooltip(self, theme, progress, card_alpha):
            wcol_pie = theme["wcol"]["pie"]
            # Root cards.
            for slot_index, entry in enumerate(self._slots):
                if not entry:
                    continue
                position = (self._positions or {}).get(slot_index)
                if position is None:
                    position = _fixed_root_position_at(self._center, slot_index)
                position = _open_position(self._visual_center, position, progress)
                label = _entry_label(entry)
                if self._card_width_limit is not None and not self._group_id:
                    label = T(("Left", "Right", "Down", "Up", "Up Left", "Up Right", "Down Left", "Down Right")[slot_index]) + " · " + label
                rect = self._slot_rect(slot_index, position)
                hovered = slot_index == self._hover_slot
                is_previous = slot_index < len(self._previous_slots) and self._previous_slots[slot_index]

                fill_color, border_color, text_color = _wheel_card_colors(wcol_pie, is_previous, hovered)
                _draw_widget(
                    rect,
                    wcol_pie,
                    theme,
                    state="hover" if hovered else "normal",
                    role="pie",
                    fill=fill_color,
                    border=border_color,
                    alpha=card_alpha,
                )
                _draw_wheel_text(
                    label,
                    (rect[0] + rect[2]) * 0.5,
                    (rect[1] + rect[3]) * 0.5,
                    11,
                    _with_alpha(text_color, card_alpha),
                    max_width=rect[2] - rect[0] - 10.0,
                )
            direct = self._card_at(self._mouse)
            content = _tip_content(self._slots[direct], self._profile) if direct >= 0 else ()
            key = (self._context_key, tuple(self._layer_path), self._group_id, direct, content) if content and progress >= 1.0 and not self._trigger_held else None
            if self._tooltip.ready(key, self._mouse):
                _draw_tip(content, self._mouse, self._bounds, theme)
CLASSES = (QCLICK2_OT_call_wheel,)


def register():
    global _REGISTERED
    _REGISTERED = True
    if clear_wheels not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(clear_wheels)


@persistent
def clear_wheels(_unused=None):
    """Cancel overlays and release handoffs before their windows are freed."""
    global _ACTIVE_WHEEL
    for pending in tuple(_PENDING_WHEELS):
        pending._cancelled = True
        pending._cleanup_action_handoff(bpy.context)
        pending._finish(bpy.context)
    active = _ACTIVE_WHEEL
    if active is not None:
        active._cancelled = True
        try:
            active._cleanup_action_handoff(bpy.context)
            active._finish(bpy.context)
        except Exception:
            pass
    _ACTIVE_WHEEL = None


def unregister():
    global _REGISTERED
    _REGISTERED = False
    if clear_wheels in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(clear_wheels)
    clear_wheels()
