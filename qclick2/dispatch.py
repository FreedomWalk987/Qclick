# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Q&Click action execution.

Port of the proven Q&Click 1.x dispatch: operator execution with context
override and contractual properties/opcontext, PROPERTY boolean toggles with
cross-editor owner resolution, and the deferred one-timer-tick dispatch with
the pointer-release barrier for operators that open transient native UI.
"""

import json

import bpy
from bpy.app.handlers import persistent

from .i18n import T
from .native_bridge import (
    OP_CONTEXT,
    SPACE_TYPE_BY_AREA,
    _canonicalize_action,
    _native_mode,
    _stored_action_entry,
    _normalize_operator_properties,
    _path_resolve_optional,
    _property_descriptor,
    _resolve_context_data_path_native_like,
)


def _context_property_owner_candidates(context):
    """Return common context owners that may contain Quick Favorite properties."""
    candidates = []
    names = (
        "scene",
        "window_manager",
        "workspace",
        "screen",
        "area",
        "region",
        "space_data",
        "tool_settings",
        "preferences",
        "object",
        "active_object",
    )
    for name in names:
        try:
            value = getattr(context, name, None)
        except Exception:
            value = None
        if value is not None and value not in candidates:
            candidates.append(value)
    return candidates


def _find_property_owner_fallback(context, prop):
    """Find an owner for a stored property id when data_path resolution fails."""
    prop = str(prop or "")
    if not prop:
        return None

    for owner in _context_property_owner_candidates(context):
        try:
            if hasattr(owner, prop):
                return owner
        except Exception:
            pass
        try:
            owner[prop]
            return owner
        except Exception:
            pass
    return None


def _property_value_get(owner, prop):
    try:
        return getattr(owner, prop)
    except Exception:
        pass

    try:
        return owner[prop]
    except Exception:
        return None


def _property_value_set(owner, prop, value):
    # RNA properties must use setattr() so Blender update callbacks run.
    if hasattr(owner, prop):
        try:
            setattr(owner, prop, value)
            return True
        except Exception:
            pass

    # Mapping-style custom properties require item assignment.
    try:
        owner[prop] = value
        return True
    except Exception:
        return False


def _tag_property_action_redraw(context):
    # The toggled property may live in a space on another screen/window (see
    # _resolve_screen_context_property_owner), so tag every open window.
    screens = []
    screen = getattr(context, "screen", None)
    if screen is not None:
        screens.append(screen)
    try:
        for window in bpy.context.window_manager.windows:
            window_screen = getattr(window, "screen", None)
            if window_screen is not None and window_screen not in screens:
                screens.append(window_screen)
    except Exception:
        pass
    for screen in screens:
        for area in getattr(screen, "areas", []) or []:
            if area.type in {"VIEW_3D", "IMAGE_EDITOR", "PROPERTIES"}:
                try:
                    area.tag_redraw()
                except Exception:
                    pass


def _push_property_undo(owner, prop):
    """Give a data toggle its own undo step, as a native property button does.

Toggles run from a timer, outside any undoable operator, so without this the
next Ctrl+Z also reverted the step before.  Screen, window and workspace
settings are not undoable in Blender either and stay out of the history.
"""
    id_data = getattr(owner, "id_data", None)
    if not isinstance(id_data, bpy.types.ID):
        return
    if isinstance(id_data, (bpy.types.Screen, bpy.types.WindowManager, bpy.types.WorkSpace)):
        return
    try:
        bpy.ops.ed.undo_push(message=f"Toggle {prop}")
    except Exception:
        pass


def _property_boolean_status(owner, prop):
    """Return (is_array, array_length) when prop is a boolean RNA property."""
    descriptor = _property_descriptor(owner, prop)
    if descriptor is None:
        return None
    if getattr(descriptor, "type", "") != "BOOLEAN":
        return None
    length = int(getattr(descriptor, "array_length", 0) or 0)
    return (length > 0, length)


# Context members that depend on which editor is under the cursor. Favorites
# recorded in one editor store paths such as ``space_data.overlay``; the
# invoking context may be a different editor, so these heads need a
# screen-wide search instead of failing on the local context.
_SCREEN_CONTEXT_HEADS = {"space_data", "area", "region", "screen", "window"}


def _resolve_screen_context_property_owner(data_path, prop, source_space_type=0):
    """Resolve editor-dependent property paths against every open area.

    Accepts only owners that expose ``prop`` as a boolean RNA property, so a
    same-named property on an unrelated space can never be toggled by mistake.
    """
    head, _, tail = str(data_path or "").partition(".")
    if head not in _SCREEN_CONTEXT_HEADS or not prop:
        return None
    try:
        wm = bpy.context.window_manager
        windows = list(getattr(wm, "windows", []) or [])
    except Exception:
        return None
    current_window = getattr(bpy.context, "window", None)
    if current_window in windows:
        windows.remove(current_window)
        windows.insert(0, current_window)

    for window in windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        if head == "screen":
            bases = [screen]
        elif head == "window":
            bases = [window]
        else:
            bases = []
            areas = list(getattr(screen, "areas", []) or [])
            if source_space_type:
                areas = [
                    area
                    for area in areas
                    if SPACE_TYPE_BY_AREA.get(str(getattr(area, "type", "") or ""))
                    == int(source_space_type)
                ]
            for area in areas:
                if head == "space_data":
                    bases.append(getattr(area.spaces, "active", None))
                elif head == "area":
                    bases.append(area)
                else:  # region
                    bases.append(next((item for item in area.regions if item.type == "WINDOW"), None))
        for base in bases:
            if base is None:
                continue
            owner = base if not tail else _path_resolve_optional(base, tail)
            if owner is None:
                continue
            if _property_boolean_status(owner, prop) is not None:
                return owner
    return None


def _toggle_property_action(context, action):
    prop = str(action.get("property", "") or "")
    index = int(action.get("index", -1))
    data_path = str(action.get("data_path", "") or "")
    source_space_type = int(action.get("source_space_type", 0) or 0)
    data_head = data_path.partition(".")[0]
    strict_screen_source = bool(
        source_space_type and data_head in _SCREEN_CONTEXT_HEADS
    )

    current_area = getattr(context, "area", None)
    current_space_type = SPACE_TYPE_BY_AREA.get(
        str(getattr(current_area, "type", "") or "")
    )

    owner = None
    if not strict_screen_source or current_space_type == source_space_type:
        owner = _resolve_context_data_path_native_like(context, data_path)
        if owner is None and context is not bpy.context:
            owner = _resolve_context_data_path_native_like(bpy.context, data_path)

    # A path can resolve to an unrelated owner when the invoking context is a
    # different editor (e.g. ``space_data.overlay`` recorded in the 3D View but
    # executed from the Image Editor resolves to the image overlay, which has
    # no such property). Treat that as unresolved instead of failing later.
    if owner is not None and _property_boolean_status(owner, prop) is None:
        if not isinstance(_property_value_get(owner, prop), bool):
            owner = None

    if owner is None:
        owner = _resolve_screen_context_property_owner(
            data_path,
            prop,
            source_space_type=source_space_type,
        )

    if owner is None and not strict_screen_source:
        fallback = _find_property_owner_fallback(context, prop)
        if fallback is None and context is not bpy.context:
            fallback = _find_property_owner_fallback(bpy.context, prop)
        # A name-only match may be a different property with the same id.
        # Require a boolean RNA descriptor (or a plain bool custom property)
        # before accepting it as the toggle target.
        if fallback is not None and _property_boolean_status(fallback, prop) is None:
            if not isinstance(_property_value_get(fallback, prop), bool):
                fallback = None
        owner = fallback

    if owner is None or not prop:
        raise RuntimeError(
            T("This property Quick Favorite cannot be resolved here: {path}", path=f"{data_path}.{prop}")
        )

    value = _property_value_get(owner, prop)
    status = _property_boolean_status(owner, prop)

    if status is not None:
        is_array, length = status
        if is_array:
            if not (0 <= index < length):
                raise RuntimeError(
                    T("Property array index out of range: {prop}[{index}] (length {length}).", prop=prop, index=index, length=length)
                )
            try:
                current = bool(value[index])
                value[index] = not current
            except Exception as exc:
                raise RuntimeError(T("This property array cannot be written now.")) from exc
        else:
            # Blender can store a spurious prop_index even for scalar
            # properties (e.g. overlay toggles); the RNA descriptor is the
            # source of truth, so toggle the scalar and ignore the index.
            if not isinstance(value, bool):
                raise RuntimeError(T("Only boolean property Quick Favorites are supported."))
            if not _property_value_set(owner, prop, not value):
                raise RuntimeError(T("This property cannot be written now."))
        _push_property_undo(owner, prop)
        _tag_property_action_redraw(context)
        return {"FINISHED"}

    # No RNA descriptor (custom property): keep the duck-typed behavior.
    use_array = index >= 0 and hasattr(value, "__len__") and not isinstance(value, (str, bytes))

    if use_array:
        try:
            current = value[index]
        except Exception as exc:
            raise RuntimeError(T("This property array index cannot be resolved here.")) from exc

        if not isinstance(current, bool):
            raise RuntimeError(T("Only boolean property Quick Favorites are supported."))

        try:
            value[index] = not current
        except Exception as exc:
            raise RuntimeError(T("This property array cannot be written now.")) from exc

        _push_property_undo(owner, prop)
        _tag_property_action_redraw(context)
        return {"FINISHED"}

    if not isinstance(value, bool):
        raise RuntimeError(T("Only boolean property Quick Favorites are supported."))

    if not _property_value_set(owner, prop, not value):
        raise RuntimeError(T("This property cannot be written now."))

    _push_property_undo(owner, prop)
    _tag_property_action_redraw(context)
    return {"FINISHED"}


# ---------------------------------------------------------------------------
# Operator execution and deferred dispatch (ported from Q&Click 1.x m05)
# ---------------------------------------------------------------------------


_DEFERRED_ACTION_CALLBACKS = set()
_LAST_ACTION_BY_CONTEXT = {}


def _repeat_context_key(context):
    area = getattr(context, "area", None)
    return f"{getattr(area, 'type', 'UNKNOWN')}|{_native_mode(context)}"


def _last_action_for_context(context):
    from . import persistence
    key = _repeat_context_key(context)
    action = _LAST_ACTION_BY_CONTEXT.get(key)
    if action is not None and not persistence.repeat_allowed_for_key(key, action):
        _LAST_ACTION_BY_CONTEXT.pop(key, None)
        return None
    return _stored_action_entry(action) if action is not None else None


def _remember_action_result(context_key, action, result):
    from . import persistence
    if not isinstance(action, dict) or action.get("kind", "ACTION") != "ACTION":
        return
    if action.get("action_type", "OPERATOR") not in {"OPERATOR", "PROPERTY"}:
        return
    # Starting a modal tool counts as selecting that tool, as in 1.x. Blender
    # does not expose arbitrary third-party modal completion via this result.
    flags = set(result or ())
    if "CANCELLED" in flags or not flags.intersection({"FINISHED", "RUNNING_MODAL"}):
        return
    if action.get("operator") == "qclick2.call_wheel":
        return
    if not persistence.repeat_allowed_for_key(context_key, action):
        return
    _LAST_ACTION_BY_CONTEXT[context_key] = _stored_action_entry(action)


@persistent
def _clear_previous_actions(_unused=None):
    _cancel_deferred_action_callbacks()
    _LAST_ACTION_BY_CONTEXT.clear()


def register_history():
    _LAST_ACTION_BY_CONTEXT.clear()
    if _clear_previous_actions not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_clear_previous_actions)


def unregister_history():
    _clear_previous_actions()
    if _clear_previous_actions in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_clear_previous_actions)


def _operator_exists(operator_id):
    if "." not in str(operator_id or ""):
        return False
    module, name = operator_id.split(".", 1)
    return hasattr(bpy.ops, module) and hasattr(getattr(bpy.ops, module), name)


def _operator_override_kwargs(context):
    area = getattr(context, "area", None)
    window = getattr(context, "window", None)
    screen = getattr(context, "screen", None)
    region = None
    if area is not None:
        region = next((item for item in area.regions if item.type == "WINDOW"), None)
    result = {}
    if window is not None:
        result["window"] = window
    if screen is not None:
        result["screen"] = screen
    if area is not None:
        result["area"] = area
    if region is not None:
        result["region"] = region
    return result


def _run_operator(context, operator_id, properties=None, opcontext=0):
    from .shortcuts import decode_value

    module, name = operator_id.split(".", 1)
    op = getattr(getattr(bpy.ops, module), name)
    kwargs = decode_value(_normalize_operator_properties(operator_id, properties))
    stored_context = OP_CONTEXT.get(int(opcontext), "INVOKE_DEFAULT")

    # Captured properties are contractual. Never retry by dropping them:
    # that silently reuses Blender's most recent operator state. The native
    # user-menu opcontext is contractual too: trying EXEC first can bypass a
    # confirmation dialog, file browser, or modal invoke path.
    # Cancellation or an exception can happen after an operator has already
    # changed data. Do not invoke it again through a different execution path.
    attempts = [(stored_context, kwargs)]
    unique_attempts = []
    seen = set()
    for execution_context, attempt_kwargs in attempts:
        key = (execution_context, json.dumps(attempt_kwargs, ensure_ascii=False, sort_keys=True, default=str))
        if key not in seen:
            seen.add(key)
            unique_attempts.append((execution_context, attempt_kwargs))

    override = _operator_override_kwargs(context)
    errors = []
    for execution_context, attempt_kwargs in unique_attempts:
        try:
            if override:
                with context.temp_override(**override):
                    result = op(execution_context, True, **attempt_kwargs)
            else:
                result = op(execution_context, True, **attempt_kwargs)
            result_set = set(result or [])
            if "CANCELLED" not in result_set:
                return result
            errors.append(f"{execution_context}: {sorted(result_set)}")
        except Exception as exc:
            errors.append(f"{execution_context}: {type(exc).__name__}: {exc}")

    detail = " | ".join(errors[-4:]) if errors else T("No details returned")
    raise RuntimeError(T("Operator failed: {id}. {detail}", id=operator_id, detail=detail))


QFAV_RELEASE_BARRIER_OPERATORS = {
    "wm.call_menu_pie",
    "wm.call_menu",
    "wm.call_panel",
    "wm.call_asset_shelf_popover",
    # Blender's built-in Fallback Tool Pie Menu (commonly bound to W / Alt+W).
    "wm.toolbar_fallback_pie",
    # Built-in enum and search launchers that create transient native UI.
    "wm.context_menu_enum",
    "wm.context_pie_enum",
    "wm.operator_pie_enum",
    "wm.search_menu",
    "wm.search_operator",
    "wm.search_single_menu",
    "wm.debug_menu",
    "wm.splash",
}
# Future Blender versions and third-party add-ons can expose additional
# UI-launching operators. These conservative patterns add the stronger handoff
# without forcing Q&Click to know every operator in advance.
QFAV_RELEASE_BARRIER_OPERATOR_PREFIXES = (
    "wm.call_",
    "wm.context_menu_",
    "wm.context_pie_",
    "wm.operator_menu_",
    "wm.operator_pie_",
    "wm.search_",
)
QFAV_RELEASE_BARRIER_OPERATOR_TOKENS = (
    "menu",
    "pie",
    "popover",
    "popup",
    "dialog",
    "search",
    "confirm",
)
# Every action waits until the source modal has consumed its selecting release
# event. Ordinary actions continue on the next timer tick; native UI launchers
# get a longer settling window.
QFAV_ACTION_HANDOFF_TIMER_INTERVAL = 0.010
QFAV_GENERAL_ACTION_HANDOFF_DELAY = 0.0
QFAV_UI_RELEASE_BARRIER_DELAY = 0.045
QFAV_NATIVE_UI_HANDOFF_DELAY = 0.12


def _operator_likely_opens_transient_ui(operator_id):
    operator_id = str(operator_id or "")
    if not operator_id:
        return False
    if operator_id in QFAV_RELEASE_BARRIER_OPERATORS:
        return True
    if any(operator_id.startswith(prefix) for prefix in QFAV_RELEASE_BARRIER_OPERATOR_PREFIXES):
        return True
    tail = operator_id.split(".", 1)[-1].lower()
    return any(token in tail for token in QFAV_RELEASE_BARRIER_OPERATOR_TOKENS)


def _action_needs_pointer_release_barrier(action):
    if not isinstance(action, dict):
        return False
    action_type = action.get("action_type", "OPERATOR")
    if action_type == "MENU":
        return True
    if action_type == "OPERATOR":
        return _operator_likely_opens_transient_ui(action.get("operator", ""))
    return False


def _run_action_entry(context, action):
    action = _canonicalize_action(action)
    action_type = action.get("action_type", "OPERATOR")
    if action_type == "OPERATOR":
        operator_id = action.get("operator", "")
        if not operator_id or not _operator_exists(operator_id):
            raise RuntimeError(T("Operator not found: {id}", id=operator_id))
        return _run_operator(context, operator_id, action.get("properties", {}), action.get("opcontext", 0))
    if action_type == "PROPERTY":
        return _toggle_property_action(context, action)
    if action_type == "MENU":
        menu_id = action.get("menu", "")
        if menu_id and hasattr(bpy.types, menu_id):
            bpy.ops.wm.call_menu(name=menu_id)
            return {"FINISHED"}
        raise RuntimeError(T("Menu not found: {id}", id=menu_id))
    raise RuntimeError(T("Unsupported Quick Favorite type: {kind}", kind=action_type))


def _captured_override_kwargs(window_pointer, screen_pointer, area_pointer, region_pointer, area_type):
    from .runtime import live_override
    return live_override(window_pointer, screen_pointer, area_pointer, region_pointer, area_type)


def _split_popup_lines(message, width=72):
    text = str(message or T("Unknown error"))
    lines = []
    while len(text) > width:
        cut = text.rfind(" ", 0, width)
        if cut < 20:
            cut = width
        lines.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        lines.append(text)
    return lines[:8]


def _show_deferred_error(message):
    detail = str(message or T("Unknown error"))
    print(f"[Q&Click] {detail}")
    try:
        wm = bpy.context.window_manager

        def draw(self, _context):
            for line in _split_popup_lines(detail):
                self.layout.label(text=line)

        wm.popup_menu(draw, title=T("Q&Click action failed"), icon="ERROR")
    except Exception:
        pass


def _schedule_action_entry(context, action, source_release_settled=False, repeat=False):
    """Run the chosen action after the wheel modal operator has returned.

    A direct nested bpy.ops call inside the wheel's modal() can prevent the
    target operator from becoming Blender's standalone last registered
    operation. Deferring by one app-timer tick lets Blender finish the wheel
    first, then invoke the target operator in the original editor WINDOW
    context.
    """
    action = _canonicalize_action(action)
    payload = json.dumps(action, ensure_ascii=False, separators=(",", ":"), default=str)
    repeat_key = _repeat_context_key(context)
    window = getattr(context, "window", None)
    screen = getattr(context, "screen", None)
    area = getattr(context, "area", None)
    region = getattr(context, "region", None)
    if area is not None:
        window_region = next((item for item in area.regions if item.type == "WINDOW"), None)
        if window_region is not None:
            region = window_region
    # Capture integers while the invocation context is still live. A ctypes
    # crash caused by reading freed Area/Region RNA cannot be caught in Python.
    window_pointer = int(window.as_pointer()) if window is not None else 0
    screen_pointer = int(screen.as_pointer()) if screen is not None else 0
    area_pointer = int(area.as_pointer()) if area is not None else 0
    region_pointer = int(region.as_pointer()) if region is not None else 0
    area_type = str(area.type) if area is not None else ""

    def callback():
        try:
            action_copy = json.loads(payload)
            live_context = bpy.context
            override = _captured_override_kwargs(window_pointer, screen_pointer, area_pointer,
                                                 region_pointer, area_type)
            if override:
                with live_context.temp_override(**override):
                    if _repeat_context_key(live_context) != repeat_key:
                        raise RuntimeError(T("The mode changed; the action was cancelled"))
                    if repeat:
                        from . import persistence
                        if not persistence.repeat_allowed(live_context, action_copy):
                            return None
                    result = _run_action_entry(live_context, action_copy)
            else:
                raise RuntimeError(T("The original editor was closed; the action was cancelled"))
            _remember_action_result(repeat_key, action_copy, result)
            # The forwarded target operator is invoked with positional
            # undo=True inside _run_operator(). This is the Blender-supported
            # path for retaining the target as the native redo / Adjust Last
            # Operation entry.
        except Exception as exc:
            _show_deferred_error(str(exc))
        finally:
            _DEFERRED_ACTION_CALLBACKS.discard(callback)
        return None

    _DEFERRED_ACTION_CALLBACKS.add(callback)
    try:
        first_interval = (
            0.0
            if source_release_settled
            else (
                QFAV_UI_RELEASE_BARRIER_DELAY
                if _action_needs_pointer_release_barrier(action)
                else 0.0
            )
        )
        bpy.app.timers.register(callback, first_interval=first_interval)
    except Exception:
        _DEFERRED_ACTION_CALLBACKS.discard(callback)
        raise


def _cancel_deferred_action_callbacks():
    """Prevent delayed actions from firing after add-on disable or reload."""
    for callback in tuple(_DEFERRED_ACTION_CALLBACKS):
        try:
            if bpy.app.timers.is_registered(callback):
                bpy.app.timers.unregister(callback)
        except Exception:
            pass
        _DEFERRED_ACTION_CALLBACKS.discard(callback)
