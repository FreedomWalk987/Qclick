# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Read-only, context-scoped shortcut capture. Never synthesize action keys."""

import json
import math
import textwrap
import time
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, StringProperty
from bpy.types import Operator

from . import native_bridge, persistence, runtime
from .i18n import T
from .runtime import live_area

MODIFIERS = {"LEFT_CTRL", "RIGHT_CTRL", "LEFT_SHIFT", "RIGHT_SHIFT", "LEFT_ALT", "RIGHT_ALT", "OSKEY"}
IGNORED = {"NONE", "ESC", "MOUSEMOVE", "INBETWEEN_MOUSEMOVE", "TEXTINPUT", "WINDOW_DEACTIVATE"}
MODE_MAPS = {
    "OBJECT": ("Object Mode",), "EDIT_MESH": ("Mesh",), "EDIT_CURVE": ("Curve",),
    "EDIT_SURFACE": ("Curve",), "EDIT_ARMATURE": ("Armature",), "POSE": ("Pose",),
    "SCULPT": ("Sculpt",), "PAINT_WEIGHT": ("Weight Paint",), "PAINT_VERTEX": ("Vertex Paint",),
    "PAINT_TEXTURE": ("Image Paint",), "EDIT_METABALL": ("Metaball",), "EDIT_LATTICE": ("Lattice",),
    "EDIT_TEXT": ("Font",), "PARTICLE": ("Particle",), "EDIT_CURVES": ("Curves",),
}
SET_TAG = "__qclick2_enum_set__"
_REGISTERED = False
_ACTIVE_CAPTURE = None
_ACTIVE_CHOOSER = None
_PENDING = {}


def encode_value(value, depth=0):
    if depth > 12:
        raise ValueError(T("Parameters are nested too deeply to capture safely"))
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(T("Parameters contain a non-finite number"))
        return value
    if isinstance(value, set):
        return {SET_TAG: sorted(encode_value(item, depth + 1) for item in value)}
    if isinstance(value, dict):
        return {str(key): encode_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, bpy.types.OperatorProperties):
        return properties_dict(value, depth + 1)
    if hasattr(value, "bl_rna"):
        raise ValueError(T("Parameters contain a Blender data pointer that cannot be saved"))
    if isinstance(value, (list, tuple)) or type(value).__name__ in {"bpy_prop_array", "Vector", "Euler", "Quaternion", "Matrix"}:
        return [encode_value(item, depth + 1) for item in value]
    raise ValueError(T("Unsupported parameter type: {name}", name=type(value).__name__))


def properties_dict(properties, depth=0):
    if isinstance(properties, dict):
        return encode_value(properties, depth)
    result = {}
    for key in properties.keys():
        value = getattr(properties, str(key)) if hasattr(properties, str(key)) else properties[key]
        result[str(key)] = encode_value(value, depth + 1)
    return result


def decode_value(value):
    if isinstance(value, dict):
        if set(value) == {SET_TAG} and isinstance(value[SET_TAG], list):
            return set(value[SET_TAG])
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def shortcut_from_event(event):
    return {"type": event.type, **{key: bool(getattr(event, key, False)) for key in ("ctrl", "shift", "alt", "oskey")}}


def shortcut_text(shortcut):
    parts = [label for key, label in (("ctrl", "Ctrl"), ("shift", "Shift"), ("alt", "Alt"), ("oskey", "OSKey")) if shortcut.get(key)]
    return " + ".join(parts + [str(shortcut.get("type", "?"))])


def recordable(event):
    name = str(event.type)
    return (event.value == "PRESS" and not getattr(event, "is_repeat", False)
            and name not in MODIFIERS | IGNORED and not name.startswith("TIMER")
            and "MOUSE" not in name and not name.startswith(("TRACKPAD", "NDOF")))


def matches(item, shortcut):
    if not item.active or item.type != shortcut["type"] or item.value not in {"PRESS", "CLICK", "ANY"}:
        return False
    if getattr(item, "key_modifier", "NONE") != "NONE":
        return False
    if getattr(item, "any", False):
        return True
    return all(int(getattr(item, key, 0)) == -1 or bool(getattr(item, key, 0)) == shortcut.get(key, False)
               for key in ("ctrl", "shift", "alt", "oskey"))


IMAGE_EDITOR_MAPS = ("UV Editor", "Image", "Image Generic", "Window", "Screen", "Frames")


def context_keymaps(context):
    if str(getattr(getattr(context, "area", None), "type", "")) == "IMAGE_EDITOR":
        # The UV/Image editor never runs the 3D View, mode or tool keymaps.
        return list(IMAGE_EDITOR_MAPS)
    names = list(MODE_MAPS.get(context.mode, ()))
    names += ["Object Non-modal", "3D View", "3D View Generic", "Window", "Screen", "Frames"]
    try:
        from bl_ui.space_toolsystem_common import keymap_from_id
        tool = context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
        for tool_id in (tool.idname, tool.idname_fallback):
            name = keymap_from_id(context, "VIEW_3D", tool_id)
            if name and name not in names:
                names.insert(0, name)
    except (AttributeError, TypeError, KeyError):
        pass
    return names


def operator_available(operator_id):
    try:
        module, name = operator_id.split(".", 1)
        return bool(getattr(getattr(bpy.ops, module), name).poll())
    except Exception:
        return False


def resolve_candidates(context, shortcut):
    """Use the effective merged user config; never resurrect disabled defaults.

    Scope is a conservative 3D View handler approximation. Poll and explicit
    confirmation prevent presenting the ranking as Blender's exact dispatcher.
    """
    config = getattr(context.window_manager.keyconfigs, "user", None)
    if config is None:
        return [], [T("The combined keymap cannot be read right now")]
    names = context_keymaps(context)
    space_types = {"EMPTY", str(getattr(getattr(context, "area", None), "type", "") or "VIEW_3D")}
    found, rejected = {}, []
    for keymap in config.keymaps:
        if keymap.name not in names or keymap.is_modal or keymap.space_type not in space_types or keymap.region_type != "WINDOW":
            continue
        for item in keymap.keymap_items:
            operator_id = item.idname
            if not matches(item, shortcut) or not operator_id or operator_id.startswith(("qclick2.", "qfav.")) or not operator_available(operator_id):
                continue
            try:
                properties = properties_dict(item.properties)
                label = native_bridge._operator_variant_default_label(operator_id, properties, fallback=item.name or operator_id)
                action = native_bridge._stored_action_entry({"kind": "ACTION", "action_type": "OPERATOR", "operator": operator_id,
                                                             "properties": properties, "label": label, "opcontext": 0})
                identity = native_bridge._action_identity(action)
                candidate = {"action": action, "keymap": keymap.name, "rank": names.index(keymap.name)}
                if identity not in found or candidate["rank"] < found[identity]["rank"]:
                    found[identity] = candidate
            except Exception as exc:
                rejected.append(T("{name}: {error}", name=operator_id, error=exc))
    return sorted(found.values(), key=lambda row: (row["rank"], row["action"]["label"], row["action"]["operator"])), rejected


def status(message):
    from . import editor
    editor._STATUS = message
    editor._tag_view3d_redraw()


def select_source(context, key, field="shortcut_id"):
    from . import editor
    rows = persistence.action_sources(context)
    editor._SELECTED_SOURCE = next((index for index, row in enumerate(rows) if row.get(field) == key), 0)
    # Match sessions by the pointers in their key, never by comparing stored
    # window/area objects that may already be stale.
    window_pointer = int(context.window.as_pointer()) if context.window else 0
    area_pointer = int(context.area.as_pointer()) if context.area else 0
    for session in editor._SESSIONS.values():
        key = session._key or (0, 0, 0)
        if key[0] == window_pointer and key[1] == area_pointer and session._source_kind == "SHORTCUTS":
            visible = session._layout.get("visible_rows", 1) if session._layout else 1
            session._scroll = max(0, editor._SELECTED_SOURCE - visible + 1)
    editor._tag_view3d_redraw()


def viewport_override(context):
    region = next((region for region in context.area.regions if region.type == "WINDOW"), None)
    return context.temp_override(window=context.window, area=context.area, region=region)


class QCLICK2_OT_capture_shortcut(Operator):
    bl_idname = "qclick2.capture_shortcut"
    bl_label = "Capture Shortcut Action"
    bl_options = {"INTERNAL", "BLOCKING"}
    _timer = None
    _shortcut = None
    _ready_at = None
    _profile = ""
    _area_pointer = 0
    _window = None
    _held_keys = None
    _cancel_requested = False

    @classmethod
    def poll(cls, context):
        return (context.area is not None
                and context.area.type in persistence.SUPPORTED_AREAS
                and context.window is not None)

    def invoke(self, context, event):
        global _ACTIVE_CAPTURE
        if not _REGISTERED or _ACTIVE_CAPTURE is not None or _PENDING:
            return {"CANCELLED"}
        self._profile = persistence._profile_key(context)
        self._area_pointer = int(context.area.as_pointer())
        self._window = context.window
        self._shortcut, self._ready_at = None, None
        self._held_keys = set()
        self._cancel_requested = False
        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        _ACTIVE_CAPTURE = self
        status(T("Press the key combination to capture; Esc or right-click cancels. The tool will not run"))
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def finish(self, context):
        global _ACTIVE_CAPTURE
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except (ReferenceError, RuntimeError):
                pass
            self._timer = None
        if _ACTIVE_CAPTURE is self:
            _ACTIVE_CAPTURE = None

    def modal(self, context, event):
        try:
            invalid = not _REGISTERED or _ACTIVE_CAPTURE is not self or persistence._profile_key(context) != self._profile
            if invalid or event.type == "WINDOW_DEACTIVATE":
                status(T("Shortcut capture cancelled"))
                self.finish(context)
                return {"CANCELLED"}
            if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
                self._cancel_requested = True
                self._held_keys.add(event.type)
                self._ready_at = None
                status(T("Cancelled; release all keys"))
                return {"RUNNING_MODAL"}
            if event.type == "MOUSEMOVE":
                area = live_area(self._area_pointer)
                if area is None or not (area.x <= event.mouse_x < area.x + area.width
                                        and area.y <= event.mouse_y < area.y + area.height):
                    status(T("Left the original editor; capture cancelled"))
                    self.finish(context)
                    return {"CANCELLED"}
            if self._shortcut is None and not self._cancel_requested:
                if recordable(event):
                    self._shortcut = shortcut_from_event(event)
                    self._held_keys.add(event.type)
                    status(T("Got {keys}; release all keys", keys=shortcut_text(self._shortcut)))
                elif event.type in MODIFIERS:
                    if event.value == "PRESS":
                        self._held_keys.add(event.type)
                    elif event.value == "RELEASE":
                        self._held_keys.discard(event.type)
                return {"RUNNING_MODAL"}
            key_event = event.type in self._held_keys or event.type in MODIFIERS or recordable(event)
            if recordable(event):
                self._held_keys.add(event.type)
            elif event.value == "RELEASE":
                self._held_keys.discard(event.type)
            if key_event:
                released = not self._held_keys and not any(getattr(event, key, False) for key in ("ctrl", "shift", "alt", "oskey"))
                self._ready_at = time.monotonic() + 0.08 if released else None
            if event.type == "TIMER" and self._ready_at is not None and time.monotonic() >= self._ready_at:
                if self._cancel_requested:
                    self.finish(context)
                    status(T("Cancelled; no shortcut source was added"))
                    return {"CANCELLED"}
                with viewport_override(context):
                    candidates, rejected = resolve_candidates(bpy.context, self._shortcut)
                self.finish(context)
                if not candidates:
                    status(T("No action to capture in this mode") + T("; ") + (rejected[0] if rejected else T("only ordinary key-press combinations are supported")))
                    return {"CANCELLED"}
                token = uuid.uuid4().hex
                _PENDING[token] = {"profile": self._profile, "window": context.window.as_pointer(), "area": context.area.as_pointer(),
                                   "shortcut": self._shortcut, "candidates": candidates}
                try:
                    result = bpy.ops.qclick2.choose_shortcut("INVOKE_DEFAULT", token=token)
                    if result != {"RUNNING_MODAL"}:
                        _PENDING.pop(token, None)
                        return {"CANCELLED"}
                except Exception:
                    _PENDING.pop(token, None)
                    raise
                status(T("Candidates found; confirm the name, operator and parameters to add the source"))
                return {"FINISHED"}
            return {"RUNNING_MODAL"}
        except Exception as exc:
            self.finish(context)
            status(T("Capture failed: {error}", error=exc))
            return {"CANCELLED"}

    def cancel(self, context):
        self.finish(context)


def candidate_items(self, context):
    # RNA property callbacks receive OperatorProperties, not the operator's
    # Python instance. Keep enum strings alive with the pending capture token.
    return _PENDING.get(getattr(self, "token", ""), {}).get("items", ())


def candidate_changed(self, context):
    data = _PENDING.get(getattr(self, "token", ""))
    if data and self.choice:
        self.label = data["candidates"][int(self.choice)]["action"]["label"]


class QCLICK2_OT_choose_shortcut(Operator):
    bl_idname = "qclick2.choose_shortcut"
    bl_label = "Confirm Shortcut Action"
    bl_options = {"INTERNAL"}
    token: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    choice: EnumProperty(name="Candidate", items=candidate_items, update=candidate_changed)
    label: StringProperty(name="Name", maxlen=96)
    _data = None

    def invoke(self, context, event):
        global _ACTIVE_CHOOSER
        self._data = _PENDING.get(self.token)
        if not self._data:
            return {"CANCELLED"}
        self._data["items"] = tuple((str(index), row["action"]["label"] + " · " + row["keymap"],
                             row["action"]["operator"] + " " + json.dumps(row["action"]["properties"], ensure_ascii=False))
                            for index, row in enumerate(self._data["candidates"]))
        self.choice = "0"
        self.label = self._data["candidates"][0]["action"]["label"]
        _ACTIVE_CHOOSER = self
        return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_MEDIUM, title=T("Confirm Shortcut Action"))

    def draw(self, context):
        if not _REGISTERED or not self._data:
            return
        self.layout.label(text=shortcut_text(self._data["shortcut"]) + " · " + persistence.profile_label(context))
        self.layout.prop(self, "choice", text=T("Candidate"))
        self.layout.prop(self, "label", text=T("Name"))
        candidate = self._data["candidates"][int(self.choice)]
        self.layout.label(text=candidate["action"]["operator"])
        details = json.dumps(candidate["action"]["properties"], ensure_ascii=False, sort_keys=True)
        for line in textwrap.wrap(details, 52)[:6]:
            self.layout.label(text=line)
        self.layout.label(text=T("Saves only the tool and parameters; the shortcut is not run"))

    def close(self):
        global _ACTIVE_CHOOSER
        _PENDING.pop(self.token, None)
        if _ACTIVE_CHOOSER is self:
            _ACTIVE_CHOOSER = None

    def execute(self, context):
        data = _PENDING.get(self.token)
        if not _REGISTERED or not data or data["profile"] != persistence._profile_key(context) or data["window"] != context.window.as_pointer() or data["area"] != context.area.as_pointer():
            self.close()
            status(T("The context changed; the shortcut source was not saved"))
            return {"CANCELLED"}
        candidate = data["candidates"][int(self.choice)]
        ok, message, key = persistence.save_shortcut(context, data["shortcut"], candidate["action"], self.label, candidate["keymap"])
        self.close()
        status(message or (T("Shortcut source added; drag it into a slot") if ok else T("Saving failed")))
        if ok:
            select_source(context, key)
        return {"FINISHED"} if ok else {"CANCELLED"}

    def cancel(self, context):
        self.close()
        status(T("Cancelled; no shortcut source was added"))


class QCLICK2_OT_rename_shortcut(Operator):
    bl_idname = "qclick2.rename_shortcut"
    bl_label = "Rename Action Source"
    bl_options = {"INTERNAL"}
    key: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    kind: EnumProperty(items=(("SHORTCUT", "Shortcut", ""), ("COMMAND", "Command", "")), default="SHORTCUT", options={"HIDDEN", "SKIP_SAVE"})
    label: StringProperty(name="Name", maxlen=96)
    _profile = ""

    def invoke(self, context, event):
        self._profile = persistence._profile_key(context)
        return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_SMALL, title=T("Rename Action Source"))

    def draw(self, context):
        self.layout.prop(self, "label", text=T("Name"))

    def execute(self, context):
        if self._profile and self._profile != persistence._profile_key(context):
            return {"CANCELLED"}
        rename = persistence.rename_command if self.kind == "COMMAND" else persistence.rename_shortcut
        ok, message = rename(context, self.key, self.label)
        status(T("Renamed the source and the copies already placed in slots") if ok else message)
        return {"FINISHED"} if ok else {"CANCELLED"}


@persistent
def clear_pending(*args):
    global _ACTIVE_CHOOSER
    if _ACTIVE_CAPTURE is not None:
        _ACTIVE_CAPTURE.finish(bpy.context)
    _PENDING.clear()
    _ACTIVE_CHOOSER = None


def register():
    global _REGISTERED
    _REGISTERED = True
    if clear_pending not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(clear_pending)


def unregister():
    global _REGISTERED
    _REGISTERED = False
    clear_pending()
    if clear_pending in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(clear_pending)


CLASSES = (QCLICK2_OT_capture_shortcut, QCLICK2_OT_choose_shortcut, QCLICK2_OT_rename_shortcut)
