# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Q&Click runtime main wheel: a native Blender pie menu over the 8 slots.

Native pie item order is W, E, S, N, NW, NE, SW, SE — identical to the slot
order the editor stores, so slot N in the editor is direction N in the wheel.
"""

import json

import bpy
from bpy.props import StringProperty
from bpy.types import Menu, Operator

from . import persistence
from .i18n import T
from .native_bridge import OP_CONTEXT, _operator_exists
from .dispatch import _toggle_property_action

DEFAULT_KEYMAP = ("Q", {})

_KEYMAP_ENTRIES = []


class QCLICK2_OT_toggle_property(Operator):
    bl_idname = "qclick2.toggle_property"
    bl_label = "Toggle Property"
    bl_description = "Toggle this property Quick Favorite"
    bl_options = {"INTERNAL"}

    payload: StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})

    def execute(self, context):
        try:
            action = json.loads(self.payload)
        except Exception:
            self.report({"ERROR"}, T("Property Quick Favorite data is damaged."))
            return {"CANCELLED"}
        try:
            return _toggle_property_action(context, action)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


def _set_operator_properties(props, properties):
    from .shortcuts import decode_value

    for key, value in (properties or {}).items():
        if str(key).startswith("_"):
            continue
        try:
            value = decode_value(value)
            target = getattr(props, str(key), None)
            if isinstance(value, dict) and isinstance(target, bpy.types.OperatorProperties):
                _set_operator_properties(target, value)
            else:
                setattr(props, str(key), value)
        except Exception:
            pass


def _draw_slot(pie, entry, profile=None):
    if not isinstance(entry, dict):
        pie.separator()
        return
    label = persistence.display_label(entry, profile or {})
    if entry.get("kind") in {"FOLDER", "GROUP"}:
        row = pie.row()
        row.operator_context = "INVOKE_DEFAULT"
        props = row.operator("qclick2.call_wheel", text=label, icon="GROUP" if entry["kind"] == "GROUP" else "FILE_FOLDER")
        props.start_layer = entry["layer_id"]
        return
    action_type = str(entry.get("action_type", "OPERATOR") or "OPERATOR").upper()

    if action_type == "OPERATOR":
        operator_id = str(entry.get("operator", "") or "")
        if not operator_id or not _operator_exists(operator_id):
            column = pie.column()
            column.enabled = False
            column.label(text=label, icon="ERROR")
            return
        row = pie.row()
        row.operator_context = OP_CONTEXT.get(int(entry.get("opcontext", 0) or 0), "INVOKE_DEFAULT")
        props = row.operator(operator_id, text=label)
        _set_operator_properties(props, entry.get("properties", {}))
        return

    if action_type == "MENU":
        menu_id = str(entry.get("menu", "") or "")
        if not menu_id:
            pie.separator()
            return
        row = pie.row()
        row.operator_context = "INVOKE_DEFAULT"
        props = row.operator("wm.call_menu", text=label)
        props.name = menu_id
        return

    if action_type == "PROPERTY":
        props = pie.operator("qclick2.toggle_property", text=label)
        props.payload = json.dumps(entry, ensure_ascii=False)
        return

    pie.separator()


class QCLICK2_MT_main_pie(Menu):
    bl_idname = "QCLICK2_MT_main_pie"
    bl_label = "Q&Click"

    def draw(self, context):
        pie = self.layout.menu_pie()
        profile = persistence.load_profile(context)
        for entry in persistence.profile_slots(profile):
            _draw_slot(pie, entry, profile)


def register_keymap():
    window_manager = bpy.context.window_manager
    keyconfig = getattr(window_manager, "keyconfigs", None)
    addon_keyconfig = getattr(keyconfig, "addon", None) if keyconfig else None
    if addon_keyconfig is None:
        return
    key, modifiers = DEFAULT_KEYMAP
    # One binding per supported editor: a keymap is scoped to a space type, so
    # the UV/Image editor needs its own entry or the wheel never opens there.
    # "UV Editor" is the keymap Blender activates while the Image editor is in
    # UV mode; "Image" covers its other modes.
    for keymap_name, space_type in (("3D View", "VIEW_3D"),
                                    ("Image", "IMAGE_EDITOR"),
                                    ("UV Editor", "EMPTY")):
        keymap = addon_keyconfig.keymaps.new(name=keymap_name, space_type=space_type)
        keymap_item = keymap.keymap_items.new("qclick2.call_wheel", key, "PRESS", **modifiers)
        _KEYMAP_ENTRIES.append((keymap, keymap_item))


def unregister_keymap():
    for keymap, keymap_item in _KEYMAP_ENTRIES:
        try:
            keymap.keymap_items.remove(keymap_item)
        except Exception:
            pass
    _KEYMAP_ENTRIES.clear()


CLASSES = (
    QCLICK2_OT_toggle_property,
    QCLICK2_MT_main_pie,
)
