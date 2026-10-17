# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)
"""Interface language: English source strings, Traditional Chinese translation.

Every user-facing string in the code is English and passes through ``T()`` when
it is shown, so the language follows Blender (Preferences > Interface >
Translation) unless the add-on preference "Interface Language" forces one.

``T("Placed in {slot}: {label}", slot=a, label=b)`` translates first, then
formats, so translations keep the same ``{name}`` placeholders.  Strings in
class attributes (``bl_label``, property names) are English; Blender
translates them through the same table, registered below.
"""

import bpy
from bpy.app import translations as _translations

from .i18n_zh import ZH_HANT

LANGUAGE_ITEMS = (
    ("AUTO", "Automatic", "Follow Blender's interface language"),
    ("EN", "English", "Always show Q&Click in English"),
    ("ZH_HANT", "繁體中文", "Always show Q&Click in Traditional Chinese"),
)

# Contexts Blender looks strings up in: "*" for most UI text, "Operator" for
# operator names, None-equivalent default for properties.
_CONTEXTS = ("*", "Operator")


def _preference():
    try:
        addon = bpy.context.preferences.addons.get(__package__)
        return addon.preferences.language if addon else "AUTO"
    except Exception:
        return "AUTO"


def is_chinese():
    """True when Q&Click text should be shown in Chinese."""
    choice = _preference()
    if choice == "ZH_HANT":
        return True
    if choice == "EN":
        return False
    try:
        view = bpy.context.preferences.view
        if not view.use_translate_interface:
            return False
        return str(_translations.locale or "").lower().startswith("zh")
    except Exception:
        return False


def T(msgid, *args, **kwargs):
    """Translate an English UI string for display, then format it."""
    text = str(msgid)
    if is_chinese():
        text = ZH_HANT.get(text, text)
    if args or kwargs:
        try:
            text = text.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError):
            text = str(msgid).format(*args, **kwargs)
    return text


class QCLICK2_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    language: bpy.props.EnumProperty(
        name="Interface Language",
        description="Language of Q&Click's own interface",
        items=LANGUAGE_ITEMS,
        default="AUTO",
    )

    def draw(self, context):
        self.layout.prop(self, "language", text=T("Interface Language"))


def _blender_table():
    table = {}
    for english, chinese in ZH_HANT.items():
        for context in _CONTEXTS:
            table[(context, english)] = chinese
    return {"zh_HANT": table, "zh_TW": table}


CLASSES = (QCLICK2_AddonPreferences,)


def register():
    try:
        _translations.unregister(__package__)
    except Exception:
        pass
    _translations.register(__package__, _blender_table())


def unregister():
    try:
        _translations.unregister(__package__)
    except Exception:
        pass
