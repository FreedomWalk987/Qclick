# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Native action display-name and previous-action participation settings."""

import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from . import persistence, runtime, shortcuts
from .i18n import T

_REGISTERED = False
_EPOCH = 0
_ACTIVE_DIALOG = None


class QCLICK2_OT_repeat_settings(Operator):
    bl_idname = "qclick2.repeat_settings"
    bl_label = "Action Settings"
    bl_options = {"INTERNAL"}
    payload: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    participates: BoolProperty(name="Record as Last Action", default=True)
    display_name: StringProperty(name="Display Name", maxlen=96, options={"SKIP_SAVE"})
    reset_name: BoolProperty(name="Restore Original Name", default=False, options={"SKIP_SAVE"})
    _initial_name = ""
    _initial_alias = ""
    _repeatable = True
    _action = None
    _profile = ""
    _window = 0
    _area = 0
    _epoch = None

    @classmethod
    def poll(cls, context):
        return (context.area is not None
                and context.area.type in persistence.SUPPORTED_AREAS
                and context.window is not None)

    def invoke(self, context, event):
        global _ACTIVE_DIALOG
        if not _REGISTERED or _ACTIVE_DIALOG is not None:
            return {"CANCELLED"}
        try:
            self._action = json.loads(self.payload)
            key = persistence.alias_key(self._action)
        except (ValueError, TypeError):
            return {"CANCELLED"}
        self._profile = persistence._profile_key(context)
        self._window, self._area = context.window.as_pointer(), context.area.as_pointer()
        self._epoch = _EPOCH
        profile = persistence.load_profile(context)
        self.display_name = self._initial_name = persistence.display_label(self._action, profile)
        self._initial_alias = profile["aliases"].get(key, "")
        self.reset_name = False
        self._repeatable = self._action.get("action_type", "OPERATOR") in {"OPERATOR", "PROPERTY"}
        self.participates = persistence.repeat_allowed(context, self._action)
        _ACTIVE_DIALOG = self
        try:
            return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_MEDIUM, title=T("Action Settings"))
        except Exception:
            self.close()
            raise

    def check(self, context):
        return True

    def draw(self, context):
        if not self._action:
            return
        self.layout.label(text=T("Original name: {name}", name=str(self._action.get("label", T("Action")))))
        row = self.layout.row()
        row.enabled = not self.reset_name
        row.prop(self, "display_name", text=T("Display Name"))
        self.layout.prop(self, "reset_name", text=T("Restore Original Name"))
        self.layout.label(text=T("Changes only what Q&Click shows, not the native Quick Favorite or its parameters"))
        if self._repeatable:
            self.layout.prop(self, "participates", text=T("Record as Last Action"))
            self.layout.label(text=T("Unchecked: still runs, but does not replace the double-Q last action"))
            self.layout.label(text=T("Unchecking the current last action clears it"))
        self.layout.label(text=T("Shared by the same action and parameters; affects only the current mode"))
        if persistence.load_error():
            self.layout.label(text=T("Settings could not be read; saving is stopped"), icon="ERROR")

    def close(self):
        global _ACTIVE_DIALOG
        if _ACTIVE_DIALOG is self:
            _ACTIVE_DIALOG = None

    def execute(self, context):
        valid = (_REGISTERED and self._epoch == _EPOCH and self._action is not None
                 and context.window is not None and context.area is not None
                 and self._window == context.window.as_pointer() and self._area == context.area.as_pointer()
                 and self._profile == persistence._profile_key(context))
        if not valid:
            self.close()
            shortcuts.status(T("The context changed; action settings were not changed"))
            return {"CANCELLED"}
        alias = self.display_name if self.display_name != self._initial_name else None
        ok, message = persistence.set_action_settings(
            context, self._action, allowed=self.participates if self._repeatable else None,
            alias=alias, reset_alias=self.reset_name, expected_alias=self._initial_alias)
        self.close()
        shortcuts.status(message or T("Action settings updated; the name is kept separate from the native command"))
        return {"FINISHED"} if ok else {"CANCELLED"}

    def cancel(self, context):
        self.close()
        shortcuts.status(T("Cancelled; name and last-action settings are unchanged"))


@persistent
def clear_pending(*args):
    global _ACTIVE_DIALOG, _EPOCH
    _ACTIVE_DIALOG = None
    _EPOCH += 1


def register():
    global _REGISTERED
    _REGISTERED = True
    clear_pending()
    if clear_pending not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(clear_pending)


def unregister():
    global _REGISTERED
    _REGISTERED = False
    clear_pending()
    if clear_pending in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(clear_pending)


CLASSES = (QCLICK2_OT_repeat_settings,)
