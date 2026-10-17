# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Q&Click — standalone linked wheels and N-sidebar drag editor.

Custom GPU wheel runtime (hold trigger, slide, release to execute)
with the 1.x semantic sector model and source-release handoff; native pie
remains as a fallback.  Fully standalone: reads Blender's native Quick
Favorites itself and persists its own per-mode wheel layouts.
"""

bl_info = {
    "name": "Q&Click",
    "author": "FreedomWalk (Harry_Yeh)",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "3D Viewport / UV Editor > Sidebar > Q&Click; hold Q",
    "description": "Hold Q for a quick-favorites wheel, arranged in a drag-and-drop sidebar editor",
    "category": "Interface",
}

import bpy

# Reinstalling over an enabled copy leaves the previous submodules in
# sys.modules; reload any cached ones in dependency order so new code never
# imports stale siblings.
import importlib as _importlib
import sys as _sys

_previous_registration = globals().get("_registration")
if _previous_registration is not None:
    _previous_registration.stop()
    if _previous_registration.cleanups:
        raise RuntimeError("Qclick reload aborted because cleanup is incomplete")

for _submodule_name in ("i18n_zh", "i18n", "runtime", "native_bridge", "dispatch", "persistence", "shortcuts", "commands", "repeat_policy", "editor", "pie", "groups", "wheel", "legacy_import"):
    _cached = _sys.modules.get(f"{__name__}.{_submodule_name}")
    if _cached is not None:
        _importlib.reload(_cached)
del _importlib, _sys

from . import commands, dispatch, editor, i18n, legacy_import, pie, repeat_policy, shortcuts, wheel
from .runtime import Registration

_registration = Registration()


def _unregister_class(cls):
    if cls.is_registered:
        bpy.utils.unregister_class(cls)


def register():
    steps = []
    for cls in i18n.CLASSES + pie.CLASSES + wheel.CLASSES + shortcuts.CLASSES + commands.CLASSES + repeat_policy.CLASSES + editor.CLASSES + legacy_import.CLASSES:
        steps.append((lambda cls=cls: bpy.utils.register_class(cls), lambda cls=cls: _unregister_class(cls)))
    steps.extend(((pie.register_keymap, pie.unregister_keymap),
                  (dispatch.register_history, dispatch.unregister_history)))
    steps.append((i18n.register, i18n.unregister))
    steps.extend((module.register, module.unregister) for module in
                 (wheel, shortcuts, commands, repeat_policy, editor, legacy_import))
    _registration.start(steps)


def unregister():
    _registration.stop()


if __name__ == "__main__":
    register()
