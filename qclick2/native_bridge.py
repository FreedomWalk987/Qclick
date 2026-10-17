# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Q&Click native Quick Favorites bridge (Blender 4.5 / 5.0 / 5.1 / 5.2).

Standalone port of the proven Q&Click 1.x bridge: reads Blender's native Quick
Favorites directly via ctypes and mutates their order with validated, backed-up
writes.  The ctypes structs mirror Blender's own DNA C structs (UserDef /
bUserMenu / bUserMenuItem in DNA_userdef_types.h, IDProperty in DNA_ID.h).
Blender is GPL and Q&Click is GPL-3.0-or-later, so reproducing these layouts
for interoperability is license-compatible.
"""

import copy
import ctypes
import json
import re
import shutil
import struct
from datetime import datetime
from pathlib import Path

import bpy

from .i18n import T, is_chinese


NATIVE_READ_SERIES = {(4, 5), (5, 0), (5, 1), (5, 2)}
NATIVE_WRITE_SERIES = {(4, 5), (5, 0), (5, 1), (5, 2)}
EXPECTED_USER_MENUS_OFFSET_456 = 10032
EXPECTED_USER_MENUS_OFFSET_52 = 10800

SPACE_VIEW3D = 1
SPACE_PROPERTIES = 4
SPACE_IMAGE = 6
SPACE_NODE = 16
SPACE_TOPBAR = 21

SPACE_TYPE_BY_AREA = {
    "VIEW_3D": SPACE_VIEW3D,
    "PROPERTIES": SPACE_PROPERTIES,
    "IMAGE_EDITOR": SPACE_IMAGE,
    "NODE_EDITOR": SPACE_NODE,
    "TOPBAR": SPACE_TOPBAR,
}

SPACE_LABEL = {
    SPACE_VIEW3D: "3D View",
    SPACE_PROPERTIES: "Properties",
    SPACE_IMAGE: "Image / UV Editor",
    SPACE_NODE: "Node Editor",
    SPACE_TOPBAR: "Topbar",
}

MODE_CONTEXT_MAP = {
    "OBJECT": "objectmode",
    "EDIT_MESH": "mesh_edit",
    "EDIT_CURVE": "curve_edit",
    "EDIT_SURFACE": "surface_edit",
    "EDIT_TEXT": "text_edit",
    "EDIT_ARMATURE": "armature_edit",
    "EDIT_METABALL": "mball_edit",
    "EDIT_LATTICE": "lattice_edit",
    "EDIT_CURVES": "curves_edit",
    "EDIT_GREASE_PENCIL": "grease_pencil_edit",
    "POSE": "posemode",
    "SCULPT": "sculpt_mode",
    "PAINT_WEIGHT": "weightpaint",
    "PAINT_VERTEX": "vertexpaint",
    "PAINT_TEXTURE": "imagepaint",
    "PARTICLE": "particlemode",
}

USER_MENU_TYPE_OPERATOR = 2
USER_MENU_TYPE_MENU = 3
USER_MENU_TYPE_PROP = 4

IDP_STRING = 0
IDP_INT = 1
IDP_FLOAT = 2
IDP_ARRAY = 5
IDP_GROUP = 6
IDP_DOUBLE = 8
IDP_BOOLEAN = 10

OP_CONTEXT = {
    0: "INVOKE_DEFAULT",
    1: "INVOKE_REGION_WIN",
    2: "INVOKE_REGION_CHANNELS",
    3: "INVOKE_REGION_PREVIEW",
    4: "INVOKE_AREA",
    5: "INVOKE_SCREEN",
    6: "EXEC_DEFAULT",
    7: "EXEC_REGION_WIN",
    8: "EXEC_REGION_CHANNELS",
    9: "EXEC_REGION_PREVIEW",
    10: "EXEC_AREA",
    11: "EXEC_SCREEN",
}

_NATIVE_CACHE = {}
_WRITE_ENABLED = True
_LAST_WRITE_ERROR = ""
_BACKUP_PATH = ""
_DETACHED_ITEM_ADDRS = []


class NativeFavoritesError(RuntimeError):
    pass


class ListBase(ctypes.Structure):
    _fields_ = [("first", ctypes.c_void_p), ("last", ctypes.c_void_p)]


class UserDefHead_456(ctypes.Structure):
    _fields_ = [
        ("versionfile", ctypes.c_int),
        ("subversionfile", ctypes.c_int),
        ("flag", ctypes.c_int),
        ("dupflag", ctypes.c_uint),
        ("pref_flag", ctypes.c_char),
        ("savetime", ctypes.c_char),
        ("mouse_emulate_3_button_modifier", ctypes.c_char),
        ("trackpad_scroll_direction", ctypes.c_char),
        ("tempdir", ctypes.c_char * 768),
        ("fontdir", ctypes.c_char * 768),
        ("renderdir", ctypes.c_char * 1024),
        ("render_cachedir", ctypes.c_char * 768),
        ("textudir", ctypes.c_char * 768),
        ("pythondir_legacy", ctypes.c_char * 768),
        ("sounddir", ctypes.c_char * 768),
        ("i18ndir", ctypes.c_char * 768),
        ("image_editor", ctypes.c_char * 1024),
        ("text_editor", ctypes.c_char * 1024),
        ("text_editor_args", ctypes.c_char * 256),
        ("anim_player", ctypes.c_char * 1024),
        ("anim_player_preset", ctypes.c_int),
        ("v2d_min_gridsize", ctypes.c_short),
        ("timecode_style", ctypes.c_short),
        ("versions", ctypes.c_short),
        ("dbl_click_time", ctypes.c_short),
        ("_pad0", ctypes.c_char * 2),
        ("border_width", ctypes.c_char),
        ("mini_axis_type", ctypes.c_char),
        ("uiflag", ctypes.c_int),
        ("uiflag2", ctypes.c_char),
        ("gpu_flag", ctypes.c_char),
        ("_pad8", ctypes.c_char * 6),
        ("app_flag", ctypes.c_char),
        ("viewzoom", ctypes.c_char),
        ("language", ctypes.c_short),
        ("mixbufsize", ctypes.c_int),
        ("audiodevice", ctypes.c_int),
        ("audiorate", ctypes.c_int),
        ("audioformat", ctypes.c_int),
        ("audiochannels", ctypes.c_int),
        ("ui_scale", ctypes.c_float),
        ("ui_line_width", ctypes.c_int),
        ("dpi", ctypes.c_int),
        ("scale_factor", ctypes.c_float),
        ("inv_scale_factor", ctypes.c_float),
        ("pixelsize", ctypes.c_float),
        ("virtual_pixel", ctypes.c_int),
        ("scrollback", ctypes.c_int),
        ("node_margin", ctypes.c_char),
        ("node_preview_res", ctypes.c_char),
        ("transopts", ctypes.c_short),
        ("menuthreshold1", ctypes.c_short),
        ("menuthreshold2", ctypes.c_short),
        ("app_template", ctypes.c_char * 64),
        ("themes", ListBase),
        ("uifonts", ListBase),
        ("uistyles", ListBase),
        ("user_keymaps", ListBase),
        ("user_keyconfig_prefs", ListBase),
        ("addons", ListBase),
        ("autoexec_paths", ListBase),
        ("script_directories", ListBase),
        ("user_menus", ListBase),
    ]


class UserDefHead_52(ctypes.Structure):
    # Blender 5.2 adds texture_cachedir after textudir (768 bytes). The new
    # asset_access and date/time fields occupy previous padding and do not
    # change alignment. Audited against v5.2.2 DNA_userdef_types.h.
    _fields_ = []
    for _field in UserDefHead_456._fields_:
        _fields_.append(_field)
        if _field[0] == "textudir":
            _fields_.append(("texture_cachedir", ctypes.c_char * 768))
    del _field


def _userdef_cls():
    return UserDefHead_52 if _blender_series() == (5, 2) else UserDefHead_456


class BUserMenu(ctypes.Structure):
    _fields_ = [
        ("next", ctypes.c_void_p),
        ("prev", ctypes.c_void_p),
        ("space_type", ctypes.c_byte),
        ("_pad0", ctypes.c_char * 7),
        ("context", ctypes.c_char * 64),
        ("items", ListBase),
    ]


class BUserMenuItem(ctypes.Structure):
    _fields_ = [
        ("next", ctypes.c_void_p),
        ("prev", ctypes.c_void_p),
        ("ui_name", ctypes.c_char * 64),
        ("type", ctypes.c_byte),
        ("_pad0", ctypes.c_char * 7),
    ]


class BUserMenuItem_Op(ctypes.Structure):
    _fields_ = [
        ("item", BUserMenuItem),
        ("op_idname", ctypes.c_char * 64),
        ("prop", ctypes.c_void_p),
        ("op_prop_enum", ctypes.c_char * 64),
        ("opcontext", ctypes.c_byte),
        ("_pad0", ctypes.c_char * 7),
    ]


class BUserMenuItem_Menu(ctypes.Structure):
    _fields_ = [("item", BUserMenuItem), ("mt_idname", ctypes.c_char * 64)]


class BUserMenuItem_Prop(ctypes.Structure):
    _fields_ = [
        ("item", BUserMenuItem),
        ("context_data_path", ctypes.c_char * 256),
        ("prop_id", ctypes.c_char * 64),
        ("prop_index", ctypes.c_int),
        ("_pad0", ctypes.c_char * 4),
    ]


class IDPropertyData(ctypes.Structure):
    # Blender 4.5 layout: scalar `val` sits at data+24.
    _fields_ = [
        ("pointer", ctypes.c_void_p),
        ("group", ListBase),
        ("val", ctypes.c_int),
        ("val2", ctypes.c_int),
    ]


class IDPropertyData_50(ctypes.Structure):
    # Blender 5.0+ grew the IDPropertyData union by 8 bytes before the scalar
    # value: `val` moved from data+24 to data+32 while `group`/`pointer` stayed
    # put.
    _fields_ = [
        ("pointer", ctypes.c_void_p),
        ("group", ListBase),
        ("_pad_v", ctypes.c_char * 8),
        ("val", ctypes.c_int),
        ("val2", ctypes.c_int),
    ]


def _make_idproperty(data_cls):
    class _IDProperty(ctypes.Structure):
        _fields_ = [
            ("next", ctypes.c_void_p),
            ("prev", ctypes.c_void_p),
            ("type", ctypes.c_byte),
            ("subtype", ctypes.c_byte),
            ("flag", ctypes.c_short),
            ("name", ctypes.c_char * 64),
            ("_pad0", ctypes.c_char * 4),
            ("data", data_cls),
            ("len", ctypes.c_int),
            ("totallen", ctypes.c_int),
            ("ui_data", ctypes.c_void_p),
        ]
    return _IDProperty


IDProperty = _make_idproperty(IDPropertyData)        # Blender 4.5 layout (default)
IDProperty_50 = _make_idproperty(IDPropertyData_50)  # Blender 5.0+ layout


def _idproperty_cls():
    return IDProperty_50 if _blender_series() in {(5, 0), (5, 1), (5, 2)} else IDProperty


def _ptr(value):
    return int(value or 0)


def _decode_cstr(raw):
    data = raw if isinstance(raw, bytes) else bytes(raw)
    return data.split(b"\0", 1)[0].decode("utf-8", "replace")


def _translate_iface(value):
    if not value:
        return ""
    try:
        return bpy.app.translations.pgettext_iface(str(value))
    except Exception:
        return str(value)


def _label_quality(value):
    text = str(value or "").strip()
    if not text:
        return -1000
    score = 0
    if any(ord(char) > 127 for char in text):
        score += 100
    if not re.fullmatch(r"[A-Za-z0-9_./\[\]-]+", text):
        score += 20
    if " " in text:
        score += 5
    score -= min(len(text), 120) * 0.01
    return score


def _best_label(*candidates):
    values = []
    for candidate in candidates:
        text = _translate_iface(candidate).strip()
        if text and text not in values:
            values.append(text)
    return max(values, key=_label_quality) if values else ""


def _native_ui_label(custom_name, *fallbacks):
    """Keep Blender's stored button text; use heuristics only when it is empty."""
    custom_label = _translate_iface(custom_name).strip()
    return custom_label or _best_label(*fallbacks)


def _operator_python_id(c_idname):
    if "_OT_" not in c_idname:
        return c_idname
    module, operator = c_idname.split("_OT_", 1)
    return f"{module.lower()}.{operator.lower()}"


def _operator_label(c_idname):
    operator_id = _operator_python_id(c_idname)
    try:
        module, name = operator_id.split(".", 1)
        return getattr(getattr(bpy.ops, module), name).get_rna_type().name
    except Exception:
        return operator_id


def _blender_series():
    version = tuple(bpy.app.version)
    return (version[0], version[1])


def _native_require_supported():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise NativeFavoritesError(T("Editing native Quick Favorites requires 64-bit Blender."))
    if _blender_series() not in NATIVE_READ_SERIES:
        raise NativeFavoritesError(T("Unsupported Blender version: {version}", version=bpy.app.version_string))
    expected = EXPECTED_USER_MENUS_OFFSET_52 if _blender_series() == (5, 2) else EXPECTED_USER_MENUS_OFFSET_456
    if _userdef_cls().user_menus.offset != expected:
        raise NativeFavoritesError(T("Blender's UserDef layout is not as expected; Quick Favorites were not read."))


def _userdef():
    _native_require_supported()
    address = int(bpy.context.preferences.as_pointer())
    if not address:
        raise NativeFavoritesError(T("Cannot get the Blender preferences pointer."))
    userdef = ctypes.cast(address, ctypes.POINTER(_userdef_cls())).contents
    if not (0.25 <= float(userdef.ui_scale) <= 8.0):
        raise NativeFavoritesError(T("Blender preferences layout check failed."))
    _verify_userdef_against_rna(userdef)
    return userdef


def _verify_userdef_against_rna(userdef):
    """Cross-check the hand-declared UserDef layout against the running build.

The offset constants only prove this file agrees with itself; they cannot
detect a layout that is wrong for the installed Blender.  Compare fields that
RNA also exposes: ui_scale sits after every directory buffer, and the themes
and addons ListBase heads sit just ahead of user_menus.  If those match the
addresses Blender's own RNA hands out, the declared layout matches memory.
Nothing here follows an address read from the struct, so a wrong layout is
rejected before any ListBase walk or native write can touch freed or foreign
memory.
"""
    try:
        preferences = bpy.context.preferences
        rna_scale = float(preferences.view.ui_scale)
        themes = preferences.themes
        addons = preferences.addons
        theme_first = int(themes[0].as_pointer()) if len(themes) else 0
        addon_first = int(addons[0].as_pointer()) if len(addons) else 0
        addon_last = int(addons[-1].as_pointer()) if len(addons) else 0
    except Exception as exc:
        raise NativeFavoritesError(T("Cannot compare the Blender preferences layout; Quick Favorites were not read.")) from exc
    if (
        abs(float(userdef.ui_scale) - rna_scale) > 1e-6
        or not theme_first
        or not addon_first
        or _ptr(userdef.themes.first) != theme_first
        or _ptr(userdef.addons.first) != addon_first
        or _ptr(userdef.addons.last) != addon_last
    ):
        raise NativeFavoritesError(
            T("The UserDef layout of Blender {version} does not match; Quick Favorites were not read.", version=bpy.app.version_string)
        )


def _iter_menu_addresses(listbase, limit=512):
    address = _ptr(listbase.first)
    seen = set()
    while address:
        if address in seen or len(seen) >= limit:
            raise NativeFavoritesError(T("Quick Favorites menu links failed validation."))
        seen.add(address)
        yield address
        address = _ptr(ctypes.cast(address, ctypes.POINTER(BUserMenu)).contents.next)


def _iter_item_addresses(listbase, limit=4096):
    address = _ptr(listbase.first)
    seen = set()
    while address:
        if address in seen or len(seen) >= limit:
            raise NativeFavoritesError(T("Quick Favorites item links failed validation."))
        seen.add(address)
        yield address
        address = _ptr(ctypes.cast(address, ctypes.POINTER(BUserMenuItem)).contents.next)


def _native_mode(context):
    return MODE_CONTEXT_MAP.get(str(getattr(context, "mode", "") or "OBJECT"), "objectmode")


def _space_type(context):
    area = getattr(context, "area", None)
    return SPACE_TYPE_BY_AREA.get(area.type) if area else None


def _screen_context(context, space_type):
    if space_type == SPACE_NODE:
        space = getattr(context, "space_data", None)
        tree_type = getattr(space, "tree_type", "") if space else ""
        if tree_type:
            return str(tree_type)
    return _native_mode(context)


def _context_keys(context):
    space_type = _space_type(context)
    if space_type is None:
        return []
    mode = _native_mode(context)
    keys = [(space_type, _screen_context(context, space_type))]
    if space_type != SPACE_TOPBAR:
        keys.append((SPACE_TOPBAR, mode))
    if space_type == SPACE_VIEW3D:
        keys.append((SPACE_PROPERTIES, mode))
    result = []
    seen = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _all_native_menus():
    userdef = _userdef()
    return [
        (address, ctypes.cast(address, ctypes.POINTER(BUserMenu)).contents)
        for address in _iter_menu_addresses(userdef.user_menus)
    ]


def _current_native_menus(context):
    lookup = {
        (int(menu.space_type), _decode_cstr(menu.context)): (address, menu)
        for address, menu in _all_native_menus()
    }
    return [lookup[key] for key in _context_keys(context) if key in lookup]


def _read_float_from_int(value):
    return struct.unpack("f", struct.pack("I", value & 0xFFFFFFFF))[0]


def _read_double_from_data(data):
    raw = struct.pack("II", data.val & 0xFFFFFFFF, data.val2 & 0xFFFFFFFF)
    return struct.unpack("d", raw)[0]


def _decode_idproperty(address, depth=0):
    if not address or depth > 8:
        return None
    idprop_cls = _idproperty_cls()
    prop = ctypes.cast(address, ctypes.POINTER(idprop_cls)).contents
    prop_type = int(prop.type)
    if prop_type == IDP_STRING:
        pointer = _ptr(prop.data.pointer)
        if not pointer:
            return ""
        size = max(0, min(int(prop.len), 65536))
        return ctypes.string_at(pointer, size).split(b"\0", 1)[0].decode("utf-8", "replace")
    if prop_type == IDP_INT:
        return int(prop.data.val)
    if prop_type == IDP_BOOLEAN:
        return bool(prop.data.val)
    if prop_type == IDP_FLOAT:
        return float(_read_float_from_int(prop.data.val))
    if prop_type == IDP_DOUBLE:
        return float(_read_double_from_data(prop.data))
    if prop_type == IDP_GROUP:
        result = {}
        child = _ptr(prop.data.group.first)
        seen = set()
        while child and child not in seen and len(seen) < 1024:
            seen.add(child)
            child_prop = ctypes.cast(child, ctypes.POINTER(idprop_cls)).contents
            name = _decode_cstr(child_prop.name)
            value = _decode_idproperty(child, depth + 1)
            if value is not None:
                result[name] = value
            child = _ptr(child_prop.next)
        return result
    if prop_type == IDP_ARRAY:
        pointer = _ptr(prop.data.pointer)
        length = max(0, min(int(prop.len), 4096))
        subtype = int(prop.subtype)
        if not pointer or not length:
            return []
        if subtype == IDP_INT:
            return list((ctypes.c_int * length).from_address(pointer))
        if subtype == IDP_FLOAT:
            return [float(v) for v in (ctypes.c_float * length).from_address(pointer)]
        if subtype == IDP_DOUBLE:
            return [float(v) for v in (ctypes.c_double * length).from_address(pointer)]
        if subtype == IDP_BOOLEAN:
            return [bool(v) for v in (ctypes.c_byte * length).from_address(pointer)]
    return None


def _safe_operator_properties(pointer):
    if not pointer:
        return {}
    try:
        value = _decode_idproperty(pointer)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _path_resolve_optional(owner, data_path):
    """Resolve an RNA path without assuming every owner exposes path_resolve."""
    if owner is None:
        return None

    data_path = str(data_path or "").strip()
    if not data_path:
        return owner

    resolver = getattr(owner, "path_resolve", None)
    if resolver is not None:
        for args in ((data_path, False), (data_path,)):
            try:
                return resolver(*args)
            except Exception:
                pass

    current = owner
    for part in data_path.split("."):
        part = part.strip()
        if not part:
            continue
        try:
            current = getattr(current, part)
        except Exception:
            return None
    return current


def _resolve_context_data_path_native_like(context, data_path):
    """Approximate Blender's native Quick Favorites property path resolution."""
    if context is None:
        return None

    data_path = str(data_path or "").strip()
    if not data_path:
        return None

    head, separator, tail = data_path.partition(".")

    owner = None
    if head:
        try:
            owner = getattr(context, head)
        except Exception:
            owner = None

    if owner is not None:
        if not separator:
            return owner
        resolved = _path_resolve_optional(owner, tail)
        if resolved is not None:
            return resolved

    resolved = _path_resolve_optional(context, data_path)
    if resolved is not None:
        return resolved

    return None


def _property_descriptor(owner, prop_id):
    if owner is None or not prop_id:
        return None
    try:
        properties = getattr(getattr(owner, "bl_rna", None), "properties", None)
        if properties is None:
            return None
        getter = getattr(properties, "get", None)
        if getter is not None:
            return getter(prop_id)
        for item in properties:
            if getattr(item, "identifier", "") == prop_id:
                return item
    except Exception:
        return None
    return None


def _property_action_label(context, custom_name, data_path, prop_id):
    """Prefer the UI label, then RNA label, then internal property id."""
    custom_name = _translate_iface(custom_name).strip()
    if custom_name:
        return custom_name

    owner = _resolve_context_data_path_native_like(context, data_path)
    descriptor = _property_descriptor(owner, prop_id)
    descriptor_name = _translate_iface(getattr(descriptor, "name", "")).strip()

    if descriptor_name:
        return descriptor_name
    if prop_id:
        return str(prop_id)
    return T("Property")


def _operator_rna_type(operator_id):
    """Return the RNA type for a Python operator id such as mesh.merge."""
    try:
        module, name = str(operator_id or "").split(".", 1)
        return getattr(getattr(bpy.ops, module), name).get_rna_type()
    except Exception:
        return None


def _operator_exists(operator_id):
    return _operator_rna_type(operator_id) is not None


def _operator_rna_property(operator_id, property_name):
    rna = _operator_rna_type(operator_id)
    if rna is None:
        return None
    try:
        return rna.properties.get(str(property_name or ""))
    except Exception:
        return None


def _enum_label_token(value):
    """Normalize translated enum labels for language-aware exact matching."""
    text = _translate_iface(value).strip().casefold()
    return re.sub(r"[\s_\-–—:：()（）\[\]【】]+", "", text)


def _enum_items(operator_id, property_name):
    prop = _operator_rna_property(operator_id, property_name)
    try:
        return list(getattr(prop, "enum_items", []))
    except Exception:
        return []


def _enum_item_identifier(operator_id, property_name, value=None, label=""):
    """Convert stored enum values or translated UI labels to RNA identifiers."""
    items = _enum_items(operator_id, property_name)
    if not items:
        return ""

    if value is not None and value != "":
        if isinstance(value, str):
            token = value.strip()
            for item in items:
                identifier = str(getattr(item, "identifier", "") or "")
                if token == identifier:
                    return identifier
            value_token = _enum_label_token(token)
            for item in items:
                identifier = str(getattr(item, "identifier", "") or "")
                name = str(getattr(item, "name", "") or "")
                if value_token and value_token in {
                    _enum_label_token(identifier),
                    _enum_label_token(name),
                }:
                    return identifier
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = int(value)
            for item in items:
                if int(getattr(item, "value", -2147483648)) == numeric:
                    return str(getattr(item, "identifier", "") or "")

    label_token = _enum_label_token(label)
    if label_token:
        matches = []
        for item in items:
            identifier = str(getattr(item, "identifier", "") or "")
            name = str(getattr(item, "name", "") or "")
            if label_token in {
                _enum_label_token(identifier),
                _enum_label_token(name),
            }:
                matches.append(identifier)
        if len(set(matches)) == 1:
            return matches[0]
    return ""


def _normalize_operator_properties(operator_id, properties):
    """Freeze enum values as explicit RNA identifiers."""
    result = copy.deepcopy(properties) if isinstance(properties, dict) else {}
    for key, value in list(result.items()):
        identifier = _enum_item_identifier(operator_id, key, value=value)
        if identifier:
            result[str(key)] = identifier
    return result


def _normalize_known_boolean_variant_properties(operator_id, properties):
    """Materialize omitted False defaults for known menu-label variants."""
    result = copy.deepcopy(properties) if isinstance(properties, dict) else {}
    key = str(operator_id or "")
    for property_name in _BOOLEAN_OPERATOR_DEFAULT_LABELS.get(key, {}):
        result.setdefault(property_name, False)
    for property_names in _COMPOSITE_BOOLEAN_OPERATOR_DEFAULT_LABELS.get(key, {}):
        for property_name in property_names:
            result.setdefault(property_name, False)
    return result


def _infer_unique_enum_variant(operator_id, label):
    """Infer an exact enum variant only when a label has one unique RNA match."""
    rna = _operator_rna_type(operator_id)
    if rna is None:
        return None
    try:
        properties = list(rna.properties)
    except Exception:
        properties = []

    matches = []
    for prop in properties:
        if str(getattr(prop, "type", "") or "") != "ENUM":
            continue
        prop_id = str(getattr(prop, "identifier", "") or "")
        identifier = _enum_item_identifier(operator_id, prop_id, label=label)
        if prop_id and identifier:
            matches.append((prop_id, identifier))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _operator_base_display_label(operator_id):
    """Return the translated RNA label for one Python operator id."""
    rna = _operator_rna_type(operator_id)
    label = _translate_iface(getattr(rna, "name", "")).strip() if rna is not None else ""
    return label or str(operator_id or "")


def _enum_item_display_label(operator_id, property_name, identifier):
    """Return a translated, human-readable label for an enum identifier."""
    identifier = str(identifier or "")
    for item in _enum_items(operator_id, property_name):
        if str(getattr(item, "identifier", "") or "") != identifier:
            continue
        return _translate_iface(getattr(item, "name", "")).strip() or identifier
    return identifier


def _operator_variant_display_parts(operator_id, properties):
    """Collect explicit enum values that materially distinguish an operator action."""
    if not isinstance(properties, dict):
        return []
    parts = []
    for property_name, value in properties.items():
        identifier = _enum_item_identifier(operator_id, property_name, value=value)
        if not identifier:
            continue
        label = _enum_item_display_label(operator_id, property_name, identifier)
        if label and label not in parts:
            parts.append(label)
    return parts


# Blender menus reuse one operator for two actions, overriding the label and a
# boolean property (e.g. the Edge menu draws mesh.mark_sharp twice: "Mark Sharp"
# with clear=False and "Clear Sharp" with clear=True). The operator's own RNA
# label cannot tell them apart and native Quick Favorites often store an empty
# ui_name, so these pairs map each (property, value) to its real menu label.
# Package-language defaults; tuples are (value-False label, value-True label).
_BOOLEAN_OPERATOR_DEFAULT_LABELS = {
    "mesh.mark_seam": {"clear": ('標記縫線', '清除縫線')},
    "uv.mark_seam": {"clear": ('標記縫線', '清除縫線')},
    "mesh.mark_sharp": {"clear": ('標記銳利', '清除銳利')},
    "mesh.mark_freestyle_edge": {"clear": ('標記 Freestyle 邊線', '清除 Freestyle 邊線')},
    "mesh.mark_freestyle_face": {"clear": ('標記 Freestyle 面', '清除 Freestyle 面')},
    "mesh.hide": {"unselected": ('隱藏所選', '隱藏未選取項')},
    "mesh.normals_make_consistent": {"inside": ('重算外部', '重算內部')},
    "grease_pencil.interpolate_sequence": {"use_selection": ('插補序列', '插補所選序列')},
    "mesh.mod_weighted_strength": {"set": ('依面強度選取', '設定面強度')},
    "view3d.view_axis": {"align_active": ('視圖軸向', '將視圖對齊作用中項目')},
}


# Every known language pair for the same variants, so a favorite captured in one
# UI language keeps its correct variant when shown in another package language.
_BOOLEAN_OPERATOR_DEFAULT_LABEL_ALIASES = {
    "mesh.mark_seam": {"clear": (('標記縫線', '清除縫線'), ('Mark Seam', 'Clear Seam'))},
    "uv.mark_seam": {"clear": (('標記縫線', '清除縫線'), ('Mark Seam', 'Clear Seam'))},
    "mesh.mark_sharp": {"clear": (('標記銳利', '清除銳利'), ('Mark Sharp', 'Clear Sharp'))},
    "mesh.mark_freestyle_edge": {"clear": (('標記 Freestyle 邊線', '清除 Freestyle 邊線'), ('Mark Freestyle Edge', 'Clear Freestyle Edge'))},
    "mesh.mark_freestyle_face": {"clear": (('標記 Freestyle 面', '清除 Freestyle 面'), ('Mark Freestyle Face', 'Clear Freestyle Face'))},
    "mesh.hide": {"unselected": (('隱藏所選', '隱藏未選取項'), ('Hide Selected', 'Hide Unselected'))},
    "mesh.normals_make_consistent": {"inside": (('重算外部', '重算內部'), ('Recalculate Outside', 'Recalculate Inside'))},
    "grease_pencil.interpolate_sequence": {"use_selection": (('插補序列', '插補所選序列'), ('Interpolate Sequence', 'Interpolate Selected Sequence'))},
    "mesh.mod_weighted_strength": {"set": (('依面強度選取', '設定面強度'), ('Select by Face Strength', 'Set Face Strength'))},
    "view3d.view_axis": {"align_active": (('視圖軸向', '將視圖對齊作用中項目'), ('View Axis', 'Align View to Active'))},
}


_FORCE_ENUM_VARIANT_SUFFIX_OPERATORS = {
    "sculpt.mask_from_boundary",
}


# Some operators need more than one boolean to identify the visible command.
# mesh.mark_sharp combines clear with use_verts, producing four distinct menu
# actions. Tuple order is: edge mark, edge clear, vertex mark, vertex clear.
_COMPOSITE_BOOLEAN_OPERATOR_DEFAULT_LABELS = {
    "mesh.mark_sharp": {
        ("clear", "use_verts"): (
            '標記銳利',
            '清除銳利',
            '從頂點標記銳利',
            '從頂點清除銳利',
        ),
    },
}

_COMPOSITE_BOOLEAN_OPERATOR_DEFAULT_LABEL_ALIASES = {
    "mesh.mark_sharp": {
        ("clear", "use_verts"): (
            ('標記銳利', '清除銳利', '從頂點標記銳利', '從頂點清除銳利'),
            ('Mark Sharp', 'Clear Sharp', 'Mark Sharp from Vertices', 'Clear Sharp from Vertices'),
        ),
    },
}


def _language_group(groups, fallback):
    """Pick the Chinese (first) or English (second) built-in label group."""
    groups = tuple(groups or ())
    if len(groups) >= 2:
        return groups[0] if is_chinese() else groups[1]
    return fallback


def _boolean_value_index(value):
    """Return 0/1 for explicit boolean-like values, or None if unknown."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and value in {0, 1}:
        return 1 if bool(value) else 0
    return None


def _boolean_operator_alias_group(operator_id, property_name, label):
    """Return the known default-label pair matching the current UI language."""
    token = _enum_label_token(label)
    if not token:
        return None
    aliases = _BOOLEAN_OPERATOR_DEFAULT_LABEL_ALIASES.get(str(operator_id or ""), {})
    for group in aliases.get(str(property_name or ""), ()):
        for candidate in group:
            if token == _enum_label_token(candidate):
                return group
    return None


def _composite_boolean_operator_variant_default_label(operator_id, properties, current_label=""):
    """Resolve menu commands distinguished by multiple boolean properties."""
    key = str(operator_id or "")
    variants = _COMPOSITE_BOOLEAN_OPERATOR_DEFAULT_LABELS.get(key, {})
    aliases = _COMPOSITE_BOOLEAN_OPERATOR_DEFAULT_LABEL_ALIASES.get(key, {})
    for property_names, labels in variants.items():
        indices = [
            _boolean_value_index(properties.get(property_name, False))
            for property_name in property_names
        ]
        if any(index is None for index in indices):
            continue
        # (clear, use_verts) -> edge mark/clear, then vertex mark/clear.
        variant_index = int(indices[0]) + (2 * int(indices[1]))

        for candidate_label in (current_label, _operator_base_display_label(operator_id)):
            token = _enum_label_token(candidate_label)
            if not token:
                continue
            for group in aliases.get(property_names, ()):
                if any(token == _enum_label_token(candidate) for candidate in group):
                    return group[variant_index]
        return _language_group(aliases.get(property_names, ()), labels)[variant_index]
    return ""


def _boolean_operator_variant_default_label(operator_id, properties, current_label=""):
    """Return a UI-language-preserving label for explicit boolean variants."""
    if not isinstance(properties, dict):
        return ""
    composite_label = _composite_boolean_operator_variant_default_label(
        operator_id,
        properties,
        current_label,
    )
    if composite_label:
        return composite_label
    variants = _BOOLEAN_OPERATOR_DEFAULT_LABELS.get(str(operator_id or ""), {})
    for property_name, labels in variants.items():
        if property_name not in properties:
            continue
        index = _boolean_value_index(properties.get(property_name))
        if index is None:
            continue

        # Prefer the language of the label that Blender actually stored.
        group = _boolean_operator_alias_group(operator_id, property_name, current_label)
        if group:
            return group[index]

        group = _boolean_operator_alias_group(
            operator_id,
            property_name,
            _operator_base_display_label(operator_id),
        )
        if group:
            return group[index]
        aliases = _BOOLEAN_OPERATOR_DEFAULT_LABEL_ALIASES.get(str(operator_id or ""), {})
        return _language_group(aliases.get(property_name, ()), labels)[index]
    return ""


def _operator_variant_default_label(operator_id, properties, fallback=""):
    """Build a stable default label such as 'Merge · At First'."""
    boolean_label = _boolean_operator_variant_default_label(operator_id, properties, fallback)
    parts = _operator_variant_display_parts(operator_id, properties)
    if boolean_label:
        return f"{boolean_label} · {' / '.join(parts)}" if parts else boolean_label
    base = _operator_base_display_label(operator_id)
    if not parts:
        return str(fallback or base or operator_id or "")
    return f"{base} · {' / '.join(parts)}"


def _operator_label_is_generic(operator_id, label):
    token = _enum_label_token(label)
    if not token:
        return True
    candidates = {
        _enum_label_token(operator_id),
        _enum_label_token(_operator_base_display_label(operator_id)),
    }
    return token in candidates


def _boolean_operator_label_is_default(operator_id, label):
    """Return whether a label is one of the known default boolean variant names."""
    token = _enum_label_token(label)
    if not token:
        return False
    key = str(operator_id or "")
    variants = _BOOLEAN_OPERATOR_DEFAULT_LABELS.get(key, {})
    aliases = _BOOLEAN_OPERATOR_DEFAULT_LABEL_ALIASES.get(key, {})
    composite_variants = _COMPOSITE_BOOLEAN_OPERATOR_DEFAULT_LABELS.get(key, {})
    composite_aliases = _COMPOSITE_BOOLEAN_OPERATOR_DEFAULT_LABEL_ALIASES.get(key, {})
    for labels in variants.values():
        for candidate in labels:
            if token == _enum_label_token(candidate):
                return True
    for alias_groups in aliases.values():
        for group in alias_groups:
            for candidate in group:
                if token == _enum_label_token(candidate):
                    return True
    for labels in composite_variants.values():
        for candidate in labels:
            if token == _enum_label_token(candidate):
                return True
    for alias_groups in composite_aliases.values():
        for group in alias_groups:
            for candidate in group:
                if token == _enum_label_token(candidate):
                    return True
    return False


def _refresh_operator_variant_label(action, operator_id, properties):
    """Upgrade generic labels while preserving aliases and deliberate custom names."""
    if not isinstance(action, dict):
        return action

    current = str(action.get("label", "") or "")
    previous_default = str(action.get("_default_label", "") or "")
    boolean_label = _boolean_operator_variant_default_label(operator_id, properties, current)
    variant_parts = _operator_variant_display_parts(operator_id, properties)

    if not boolean_label and not variant_parts:
        action.setdefault("_default_label", current)
        return action

    force_enum_suffix = bool(
        variant_parts and operator_id in _FORCE_ENUM_VARIANT_SUFFIX_OPERATORS
    )
    enum_suffix = f" · {' / '.join(variant_parts)}" if force_enum_suffix else ""
    force_base = (
        current[:-len(enum_suffix)]
        if enum_suffix and current.endswith(enum_suffix)
        else current
    )
    default_label = (
        f"{force_base}{enum_suffix}"
        if force_enum_suffix and current
        else _operator_variant_default_label(operator_id, properties, fallback=current)
    )
    inherited_default = bool(previous_default) and current == previous_default
    is_boolean_default_label = bool(boolean_label) and _boolean_operator_label_is_default(
        operator_id,
        current,
    )
    is_variant_only_label = bool(boolean_label) and any(
        _enum_label_token(current) == _enum_label_token(part)
        for part in variant_parts
    )
    if (
        not current
        or inherited_default
        or _operator_label_is_generic(operator_id, current)
        or is_boolean_default_label
        or is_variant_only_label
        or force_enum_suffix
    ):
        action["label"] = default_label
    action["_default_label"] = default_label
    return action


def _canonicalize_action(action):
    """Return one deep, execution-safe action snapshot.

    A concrete enum variant must never degrade into a generic operator that
    silently reuses Blender's most recent state.
    """
    if not isinstance(action, dict):
        return action

    result = copy.deepcopy(action)
    if result.get("action_type", "OPERATOR") != "OPERATOR":
        return result

    operator_id = str(result.get("operator", "") or "")
    properties = _normalize_operator_properties(operator_id, result.get("properties", {}))
    # Manual source names are presentation only. Unlike native menu entries,
    # they provide no evidence for inferred enum variants or boolean defaults.
    if result.get("manual_command") is True or result.get("frozen_parameters") is True:
        result["properties"] = properties
        return result
    properties = _normalize_known_boolean_variant_properties(operator_id, properties)
    enum_property = str(result.get("enum_property", "") or "")
    label = str(result.get("label", "") or "")

    if enum_property:
        identifier = _enum_item_identifier(
            operator_id,
            enum_property,
            value=properties.get(enum_property),
            label=label,
        )
        if identifier:
            properties[enum_property] = identifier
            result["enum_property"] = ""

    if not properties:
        inferred = _infer_unique_enum_variant(operator_id, label)
        if inferred is not None:
            prop_id, identifier = inferred
            properties[prop_id] = identifier
            result["enum_property"] = ""

    result["properties"] = properties
    return _refresh_operator_variant_label(result, operator_id, properties)


def _stored_action_entry(action, label=None, **extra):
    """Build a canonical ACTION snapshot for wheel slots."""
    action = _canonicalize_action(action)
    entry = {
        "kind": "ACTION",
        "label": label if label is not None else action.get("label", action.get("operator", "")),
        "_default_label": action.get("_default_label", action.get("label", action.get("operator", ""))),
        "action_type": action.get("action_type", "OPERATOR"),
        "operator": action.get("operator", ""),
        "menu": action.get("menu", ""),
        "data_path": action.get("data_path", ""),
        "property": action.get("property", ""),
        "index": int(action.get("index", -1)),
        "source_space_type": int(action.get("source_space_type", 0) or 0),
        "properties": copy.deepcopy(action.get("properties", {})),
        "opcontext": int(action.get("opcontext", 0)),
        "enum_property": action.get("enum_property", ""),
    }
    if action.get("manual_command") is True:
        entry["manual_command"] = True
    if action.get("frozen_parameters") is True:
        entry["frozen_parameters"] = True
    entry.update(extra)
    return entry


def _native_item_action(item_address, source, menu_address=0, context=None, source_space_type=0):
    base = ctypes.cast(item_address, ctypes.POINTER(BUserMenuItem)).contents
    native_meta = {
        "_native_menu_addr": hex(int(menu_address or 0)),
        "_native_item_addr": hex(int(item_address or 0)),
        "source_space_type": int(source_space_type or 0),
    }
    item_type = int(base.type)
    custom_name = _decode_cstr(base.ui_name)

    if item_type == USER_MENU_TYPE_OPERATOR:
        item = ctypes.cast(item_address, ctypes.POINTER(BUserMenuItem_Op)).contents
        c_idname = _decode_cstr(item.op_idname)
        operator_id = _operator_python_id(c_idname)
        return _canonicalize_action({
            "kind": "ACTION",
            "action_type": "OPERATOR",
            "label": _native_ui_label(custom_name, _operator_label(c_idname), operator_id),
            "source": source,
            "operator": operator_id,
            "properties": _safe_operator_properties(_ptr(item.prop)),
            "opcontext": int(item.opcontext),
            "enum_property": _decode_cstr(item.op_prop_enum),
            **native_meta,
        })

    if item_type == USER_MENU_TYPE_MENU:
        item = ctypes.cast(item_address, ctypes.POINTER(BUserMenuItem_Menu)).contents
        menu_id = _decode_cstr(item.mt_idname)
        return {
            "kind": "ACTION",
            "action_type": "MENU",
            "label": _native_ui_label(custom_name, menu_id),
            "source": source,
            "menu": menu_id,
            **native_meta,
        }

    if item_type == USER_MENU_TYPE_PROP:
        item = ctypes.cast(item_address, ctypes.POINTER(BUserMenuItem_Prop)).contents
        data_path = _decode_cstr(item.context_data_path)
        prop_id = _decode_cstr(item.prop_id)
        return {
            "kind": "ACTION",
            "action_type": "PROPERTY",
            "label": _property_action_label(
                context,
                custom_name,
                data_path,
                prop_id,
            ),
            "source": source,
            "data_path": data_path,
            "property": prop_id,
            "index": int(item.prop_index),
            **native_meta,
        }
    return None


def _action_identity(action):
    action = _canonicalize_action(action)
    action_type = action.get("action_type", "OPERATOR")
    if action_type == "OPERATOR":
        return ("OPERATOR", action.get("operator", ""), json.dumps(action.get("properties", {}), sort_keys=True))
    if action_type == "MENU":
        return ("MENU", action.get("menu", ""))
    if action_type == "PROPERTY":
        index = int(action.get("index", -1))
        # Blender may store scalar booleans as either -1 or a spurious 0. They
        # execute identically; arrays still keep index 1+ distinct from index 0.
        identity_index = -1 if index <= 0 else index
        return ("PROPERTY", action.get("data_path", ""), action.get("property", ""), identity_index)
    return (action_type, action.get("label", ""))


def _native_context_cache_key(context):
    return tuple(_context_keys(context))


def _invalidate_native_cache():
    _NATIVE_CACHE.clear()


def _native_quick_favorites(context, force_refresh=False):
    cache_key = _native_context_cache_key(context)
    if not force_refresh and cache_key in _NATIVE_CACHE:
        return copy.deepcopy(_NATIVE_CACHE[cache_key])

    actions = []
    seen = set()
    for menu_address, menu in _current_native_menus(context):
        source = f"{SPACE_LABEL.get(int(menu.space_type), str(int(menu.space_type)))} · {_decode_cstr(menu.context)}"
        for item_address in _iter_item_addresses(menu.items):
            action = _native_item_action(
                item_address,
                source,
                menu_address,
                context=context,
                source_space_type=int(menu.space_type),
            )
            if not action:
                continue
            identity = _action_identity(action)
            if identity in seen:
                continue
            seen.add(identity)
            actions.append(action)

    _NATIVE_CACHE[cache_key] = copy.deepcopy(actions)
    return actions


# ---------------------------------------------------------------------------
# Native mutation (reorder / remove) with validation, backup and rollback
# ---------------------------------------------------------------------------


def _native_item_ptr(address):
    return ctypes.cast(int(address), ctypes.POINTER(BUserMenuItem))


def _native_bridge_support_error():
    try:
        _native_require_supported()
    except Exception as exc:
        return exc
    return None


def _native_bridge_can_write():
    global _LAST_WRITE_ERROR
    if not _WRITE_ENABLED:
        return False
    if _blender_series() not in NATIVE_WRITE_SERIES:
        _LAST_WRITE_ERROR = (
            T("This Blender version ({version}) does not support editing native Quick Favorites (reorder/remove) yet.", version=bpy.app.version_string)
        )
        return False
    support_error = _native_bridge_support_error()
    if support_error is not None:
        _LAST_WRITE_ERROR = str(support_error)
        return False
    return True


def _native_bridge_lock_read_only(error):
    """Lock native mutation for this Blender session after a guard failure."""
    global _WRITE_ENABLED, _LAST_WRITE_ERROR
    _WRITE_ENABLED = False
    _LAST_WRITE_ERROR = str(error or T("Native Quick Favorites editing is now read-only."))


def _native_bridge_status_text():
    if _native_bridge_can_write():
        return T("Native Quick Favorites: writable")
    detail = _LAST_WRITE_ERROR.strip()
    return T("Native Quick Favorites: read-only - {detail}", detail=detail) if detail else T("Native Quick Favorites: read-only")


def _find_native_menu_by_addr(menu_address):
    for address, menu in _all_native_menus():
        if int(address) == int(menu_address):
            return menu
    raise NativeFavoritesError(T("The selected native Quick Favorites menu no longer exists; refresh."))


def _rewrite_native_item_chain(listbase, ordered_addresses):
    if not ordered_addresses:
        listbase.first = None
        listbase.last = None
        return

    listbase.first = int(ordered_addresses[0])
    listbase.last = int(ordered_addresses[-1])
    for index, address in enumerate(ordered_addresses):
        item = _native_item_ptr(address).contents
        item.prev = int(ordered_addresses[index - 1]) if index > 0 else None
        item.next = int(ordered_addresses[index + 1]) if index + 1 < len(ordered_addresses) else None


def _native_item_signature(address, context=None):
    """Compact identity used to validate ctypes Quick Favorites mutations."""
    try:
        action = _native_item_action(int(address), "", 0, context=context)
        if action:
            return _action_identity(action)
    except Exception:
        pass
    try:
        base = _native_item_ptr(address).contents
        return ("RAW", int(base.type), _decode_cstr(base.ui_name))
    except Exception as exc:
        raise NativeFavoritesError(T("Cannot read the native Quick Favorite item: {address}", address=address)) from exc


def _native_item_signatures(addresses, context=None):
    return [_native_item_signature(address, context=context) for address in addresses]


def _validate_native_item_chain(menu, expected_addresses=None, expected_signatures=None, context=None):
    """Validate listbase order plus prev/next pointers before saving userpref."""
    addresses = list(_iter_item_addresses(menu.items))
    if expected_addresses is not None and addresses != [int(address) for address in expected_addresses]:
        raise NativeFavoritesError(T("Native Quick Favorites order failed validation; nothing was saved."))

    if addresses:
        if _ptr(menu.items.first) != int(addresses[0]) or _ptr(menu.items.last) != int(addresses[-1]):
            raise NativeFavoritesError(T("Native Quick Favorites list head/tail failed validation; nothing was saved."))
    elif _ptr(menu.items.first) or _ptr(menu.items.last):
        raise NativeFavoritesError(T("Native Quick Favorites empty-list pointers failed validation; nothing was saved."))

    for index, address in enumerate(addresses):
        item = _native_item_ptr(address).contents
        expected_prev = int(addresses[index - 1]) if index > 0 else 0
        expected_next = int(addresses[index + 1]) if index + 1 < len(addresses) else 0
        if _ptr(item.prev) != expected_prev or _ptr(item.next) != expected_next:
            raise NativeFavoritesError(T("Native Quick Favorites linked-list pointers failed validation; nothing was saved."))

    if expected_signatures is not None:
        signatures = _native_item_signatures(addresses, context=context)
        if signatures != list(expected_signatures):
            raise NativeFavoritesError(T("Native Quick Favorites contents failed validation; nothing was saved."))

    return addresses


def _restore_native_item_chain(menu, original_addresses, original_signatures, context=None):
    _rewrite_native_item_chain(menu.items, original_addresses)
    _validate_native_item_chain(
        menu,
        expected_addresses=original_addresses,
        expected_signatures=original_signatures,
        context=context,
    )


def _preferences_dirty():
    try:
        return bool(bpy.context.preferences.is_dirty)
    except Exception:
        # Unknown: assume other edits may be pending and do not force a save.
        return True


def _save_user_preferences():
    try:
        bpy.ops.wm.save_userpref()
    except Exception as exc:
        raise NativeFavoritesError(T("The running data was changed, but Blender preferences could not be saved: {error}", error=exc)) from exc


BACKUP_KEEP = 10
BACKUP_PREFIX = "userpref_before_qclick2_edit_"


def _prune_preference_backups(backup_dir, keep=BACKUP_KEEP):
    """Keep the newest backups this add-on made; never touch other files."""
    try:
        ours = sorted((path for path in backup_dir.glob(BACKUP_PREFIX + "*.blend") if path.is_file()),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in ours[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


def _backup_user_preferences_once():
    """One-time backup per Blender process before native mutation.

Copies the preferences file as it is on disk -- the last state the user saved.
It deliberately does not save first: saving would also write every other
preference the user changed but has not chosen to keep.
"""
    global _BACKUP_PATH
    if _BACKUP_PATH:
        return _BACKUP_PATH, False

    config_dir = Path(bpy.utils.user_resource("CONFIG"))
    pref_path = config_dir / "userpref.blend"
    if not pref_path.exists():
        # Preferences were never saved: there is no on-disk state to protect.
        return "", False

    backup_dir = config_dir / "qclick2_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{BACKUP_PREFIX}{stamp}.blend"
    shutil.copy2(pref_path, backup_path)
    _prune_preference_backups(backup_dir)
    _BACKUP_PATH = str(backup_path)
    return _BACKUP_PATH, True


def _action_native_addresses(action):
    menu_address = str(action.get("_native_menu_addr", "") or "")
    item_address = str(action.get("_native_item_addr", "") or "")
    if not menu_address or not item_address:
        raise NativeFavoritesError(T("This is not a native Blender Quick Favorite, so it cannot be reordered or removed."))
    try:
        return int(menu_address, 16), int(item_address, 16)
    except Exception as exc:
        raise NativeFavoritesError(T("Native Quick Favorite address data is damaged; refresh.")) from exc


def _mutate_native_action(context, action, operation):
    """Reorder or remove one native Quick Favorite; returns a status message."""
    if not _native_bridge_can_write():
        raise NativeFavoritesError(_native_bridge_status_text())

    menu_address, item_address = _action_native_addresses(action)
    menu = _find_native_menu_by_addr(menu_address)
    original_order = _validate_native_item_chain(menu, context=context)
    original_signatures = _native_item_signatures(original_order, context=context)
    if item_address not in original_order:
        raise NativeFavoritesError(T("The selected native Quick Favorite no longer exists; refresh."))

    ordered = list(original_order)
    index = ordered.index(item_address)
    changed = False
    if operation == "TOP" and index > 0:
        ordered.insert(0, ordered.pop(index))
        changed = True
    elif operation == "UP" and index > 0:
        ordered[index - 1], ordered[index] = ordered[index], ordered[index - 1]
        changed = True
    elif operation == "DOWN" and index + 1 < len(ordered):
        ordered[index + 1], ordered[index] = ordered[index], ordered[index + 1]
        changed = True
    elif operation == "BOTTOM" and index + 1 < len(ordered):
        ordered.append(ordered.pop(index))
        changed = True
    elif operation == "REMOVE":
        ordered.remove(item_address)
        changed = True

    if not changed:
        return T("Nothing to change.")

    expected_signatures_by_address = {
        int(address): signature
        for address, signature in zip(original_order, original_signatures)
    }
    expected_signatures = [
        expected_signatures_by_address[int(address)]
        for address in ordered
    ]

    # Back up before touching Blender's runtime linked-list pointers.
    _, backup_created = _backup_user_preferences_once()
    # Read before touching anything: if the user already has unsaved
    # preference edits, saving now would commit those too, without asking.
    others_pending = _preferences_dirty()
    try:
        _rewrite_native_item_chain(menu.items, ordered)
        _validate_native_item_chain(
            menu,
            expected_addresses=ordered,
            expected_signatures=expected_signatures,
            context=context,
        )
        if not others_pending:
            # Only this change is pending, so saving writes nothing else.
            _save_user_preferences()
            _validate_native_item_chain(
                menu,
                expected_addresses=ordered,
                expected_signatures=expected_signatures,
                context=context,
            )
    except Exception as exc:
        try:
            _restore_native_item_chain(
                menu,
                original_order,
                original_signatures,
                context=context,
            )
        except Exception as restore_exc:
            lock_message = (
                T("Changing native Quick Favorites failed and the automatic restore failed too. Restart Blender and restore preferences from the backup: {error}", error=restore_exc)
            )
            _native_bridge_lock_read_only(lock_message)
            raise NativeFavoritesError(lock_message) from restore_exc
        _native_bridge_lock_read_only(exc)
        raise

    if operation == "REMOVE":
        detached = _native_item_ptr(item_address).contents
        detached.prev = None
        detached.next = None
        _DETACHED_ITEM_ADDRS.append(item_address)

    _invalidate_native_cache()
    message = T("Removed the native Quick Favorite.") if operation == "REMOVE" else T("Updated the native Quick Favorites order.")
    if others_pending:
        # Preferences are already marked changed, so this edit is kept with the
        # others: by auto-save on quit, or when the user saves preferences.
        try:
            auto_save = bool(bpy.context.preferences.use_preferences_save)
        except Exception:
            auto_save = False
        if auto_save:
            message += " " + T("Preferences have other unsaved changes; this change will be auto-saved with them when Blender quits.")
        else:
            message += " " + T("Preferences have other unsaved changes, so nothing was saved; use Save Preferences to keep it.")
    if backup_created:
        message += " " + T("A one-time preferences backup was made.")
    return message
