# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Manual operator sources. Validate identifiers without running Python/tools."""

import re

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty
from bpy.types import Operator

from . import persistence, runtime, shortcuts
from .i18n import T

_IDENTIFIER = re.compile(r"(?:bpy\.ops\.)?([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)(?:\(\s*\))?")
_REGISTERED = False
_EPOCH = 0
_ACTIVE_DIALOG = None


def inspect_command(value):
    """Return a canonical operator ID and RNA label; do not poll or invoke."""
    match = _IDENTIFIER.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(T("Enter an operator ID such as transform.translate; code and calls with arguments are not accepted"))
    operator_id = match.group(1)
    if operator_id.startswith(("qclick2.", "qfav.")):
        raise ValueError(T("Q&Click's own control operators cannot be added to the wheel"))
    module, name = operator_id.split(".", 1)
    try:
        rna = getattr(getattr(bpy.ops, module), name).get_rna_type()
    except Exception:
        raise ValueError(T("No registered operator: {id}", id=operator_id)) from None
    return operator_id, str(rna.name or operator_id)


class QCLICK2_OT_add_command(Operator):
    bl_idname = "qclick2.add_command"
    bl_label = "Add Manual Command"
    bl_options = {"INTERNAL"}
    operator_id: StringProperty(name="Operator ID", maxlen=160, options={"SKIP_SAVE"})
    label: StringProperty(name="Name", maxlen=96, options={"SKIP_SAVE"})
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
        if not _REGISTERED or _ACTIVE_DIALOG is not None or shortcuts._ACTIVE_CAPTURE is not None or shortcuts._PENDING:
            return {"CANCELLED"}
        self._profile = persistence._profile_key(context)
        self._window, self._area = context.window.as_pointer(), context.area.as_pointer()
        self._epoch = _EPOCH
        _ACTIVE_DIALOG = self
        try:
            return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_MEDIUM, title=T("Add Manual Command"))
        except Exception:
            self.close()
            raise

    def check(self, context):
        return True

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "operator_id", text=T("Operator ID"))
        layout.prop(self, "label", text=T("Name"))
        if self.operator_id.strip():
            try:
                _, label = inspect_command(self.operator_id)
                layout.label(text=label, icon="CHECKMARK")
            except ValueError:
                layout.label(text=T("Not recognised: use a registered operator ID"), icon="ERROR")
        else:
            layout.label(text=T("For example: transform.translate"))
        layout.label(text=T("Leave the name empty to use Blender's tool name"))
        layout.label(text=T("Only adds a source; runs neither the tool nor any Python"))
        layout.label(text=T("For a version with fixed parameters, use Capture"))

    def close(self):
        global _ACTIVE_DIALOG
        if _ACTIVE_DIALOG is self:
            _ACTIVE_DIALOG = None

    def execute(self, context):
        if not _REGISTERED or (self._epoch is not None and (
                self._epoch != _EPOCH or self._profile != persistence._profile_key(context)
                or context.window is None or context.area is None
                or self._window != context.window.as_pointer() or self._area != context.area.as_pointer())):
            self.close()
            shortcuts.status(T("The context changed; the manual command was not added"))
            return {"CANCELLED"}
        ok, message, key = persistence.save_command(context, self.operator_id, self.label)
        self.close()
        shortcuts.status(message or (T("Command added; drag it into a slot") if ok else T("Saving failed")))
        if ok:
            shortcuts.select_source(context, key, field="command_id")
        return {"FINISHED"} if ok else {"CANCELLED"}

    def cancel(self, context):
        self.close()
        shortcuts.status(T("Cancelled; no manual command was added"))


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


CLASSES = (QCLICK2_OT_add_command,)

