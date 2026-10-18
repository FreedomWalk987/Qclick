# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Export and import Blender's native Quick Favorites with the wheel settings.

Export walks Blender's own Quick Favorites lists (through the validated
native_bridge structs) and writes every menu and item, operator properties
included, as plain JSON.

Blender has no API that adds a Quick Favorite, and its lists must only ever
hold memory Blender allocated itself.  So import never leaves foreign memory
in Blender's lists: it builds the items in Python-owned memory, links them in
only while Blender writes userpref.blend, puts the original lists back
straight away, then asks Blender to re-read the saved preferences.  Blender
reads the file into its own allocations, the same as at start-up.  The
preferences file is backed up first.
"""

import ctypes
import shutil
import struct
from datetime import datetime
from pathlib import Path

import bpy

from . import native_bridge as nb
from .i18n import T


FORMAT_KEY = "quick_favorites"
USER_MENU_TYPE_SEP = 1
TYPE_NAMES = {
    USER_MENU_TYPE_SEP: "SEPARATOR",
    nb.USER_MENU_TYPE_OPERATOR: "OPERATOR",
    nb.USER_MENU_TYPE_MENU: "MENU",
    nb.USER_MENU_TYPE_PROP: "PROPERTY",
}
TYPE_IDS = {name: value for value, name in TYPE_NAMES.items()}
IDP_NAMES = {
    nb.IDP_STRING: "STRING",
    nb.IDP_INT: "INT",
    nb.IDP_FLOAT: "FLOAT",
    nb.IDP_DOUBLE: "DOUBLE",
    nb.IDP_BOOLEAN: "BOOLEAN",
    nb.IDP_GROUP: "GROUP",
    nb.IDP_ARRAY: "ARRAY",
}
IDP_IDS = {name: value for value, name in IDP_NAMES.items()}
ARRAY_CTYPES = {
    nb.IDP_INT: ctypes.c_int,
    nb.IDP_FLOAT: ctypes.c_float,
    nb.IDP_DOUBLE: ctypes.c_double,
    nb.IDP_BOOLEAN: ctypes.c_byte,
}
NAME_MAX = 63
DATA_PATH_MAX = 255
MAX_MENUS = 256
MAX_ITEMS = 1024
BACKUP_PREFIX = "userpref_before_qclick2_import_"


class FavoritesImportError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _encode_idproperty(address, depth=0):
    if not address or depth > 8:
        return None
    prop = ctypes.cast(address, ctypes.POINTER(nb._idproperty_cls())).contents
    prop_type = int(prop.type)
    if prop_type == nb.IDP_GROUP:
        items = []
        child = nb._ptr(prop.data.group.first)
        seen = set()
        while child and child not in seen and len(seen) < 1024:
            seen.add(child)
            child_prop = ctypes.cast(child, ctypes.POINTER(nb._idproperty_cls())).contents
            encoded = _encode_idproperty(child, depth + 1)
            if encoded is not None:
                encoded["name"] = nb._decode_cstr(child_prop.name)
                items.append(encoded)
            child = nb._ptr(child_prop.next)
        return {"type": "GROUP", "items": items}
    if prop_type == nb.IDP_ARRAY:
        subtype = int(prop.subtype)
        if subtype not in ARRAY_CTYPES:
            return None
        value = nb._decode_idproperty(address, depth)
        return {"type": "ARRAY", "subtype": IDP_NAMES[subtype], "value": value if isinstance(value, list) else []}
    if prop_type == nb.IDP_STRING:
        return {"type": "STRING", "subtype": int(prop.subtype), "value": nb._decode_idproperty(address, depth) or ""}
    if prop_type in (nb.IDP_INT, nb.IDP_FLOAT, nb.IDP_DOUBLE, nb.IDP_BOOLEAN):
        return {"type": IDP_NAMES[prop_type], "value": nb._decode_idproperty(address, depth)}
    return None


def _encode_item(item_address):
    base = ctypes.cast(item_address, ctypes.POINTER(nb.BUserMenuItem)).contents
    type_name = TYPE_NAMES.get(int(base.type))
    if type_name is None:
        return None
    entry = {"type": type_name, "ui_name": nb._decode_cstr(base.ui_name)}
    if type_name == "OPERATOR":
        item = ctypes.cast(item_address, ctypes.POINTER(nb.BUserMenuItem_Op)).contents
        entry.update(
            op_idname=nb._decode_cstr(item.op_idname),
            op_prop_enum=nb._decode_cstr(item.op_prop_enum),
            opcontext=int(item.opcontext),
            properties=_encode_idproperty(nb._ptr(item.prop)),
        )
    elif type_name == "MENU":
        item = ctypes.cast(item_address, ctypes.POINTER(nb.BUserMenuItem_Menu)).contents
        entry["mt_idname"] = nb._decode_cstr(item.mt_idname)
    elif type_name == "PROPERTY":
        item = ctypes.cast(item_address, ctypes.POINTER(nb.BUserMenuItem_Prop)).contents
        entry.update(
            context_data_path=nb._decode_cstr(item.context_data_path),
            prop_id=nb._decode_cstr(item.prop_id),
            prop_index=int(item.prop_index),
        )
    return entry


def export_favorites():
    """Every native Quick Favorites menu as JSON-ready data: (menus, error)."""
    try:
        menus = []
        for _address, menu in nb._all_native_menus():
            items = [entry for entry in (_encode_item(address) for address in nb._iter_item_addresses(menu.items)) if entry]
            menus.append({"space_type": int(menu.space_type), "context": nb._decode_cstr(menu.context), "items": items})
        return menus, ""
    except Exception as exc:
        return None, str(exc)


def favorites_count(menus):
    return sum(len(menu.get("items", ())) for menu in menus or ())


# ---------------------------------------------------------------------------
# Validation of imported data
# ---------------------------------------------------------------------------


def _operator_c_idname(operator_id):
    """'mesh.mark_seam' -> 'MESH_OT_mark_seam'; C names pass through."""
    if "_OT_" in operator_id:
        return operator_id
    module, _sep, name = operator_id.partition(".")
    if not module or not name:
        raise FavoritesImportError(T("Invalid operator name: {name}", name=operator_id))
    return f"{module.upper()}_OT_{name}"


def _operator_rna(c_idname):
    module, _sep, name = c_idname.partition("_OT_")
    try:
        return getattr(getattr(bpy.ops, module.lower()), name).get_rna_type()
    except Exception:
        return None


def _typed_from_rna(c_idname, values):
    """Plain {name: value} operator properties -> typed GROUP, using the operator's RNA."""
    rna = _operator_rna(c_idname)
    items = []
    for name, value in values.items():
        prop = rna.properties.get(name) if rna is not None else None
        kind = prop.type if prop is not None else ""
        if kind == "ENUM" and isinstance(value, str):
            enum_items = prop.enum_items
            if value not in enum_items:
                raise FavoritesImportError(T("{operator}: unknown option {value}", operator=c_idname, value=value))
            items.append({"name": name, "type": "INT", "value": int(enum_items[value].value)})
        elif kind == "BOOLEAN" or isinstance(value, bool):
            if isinstance(value, (list, tuple)):
                items.append({"name": name, "type": "ARRAY", "subtype": "BOOLEAN", "value": [bool(v) for v in value]})
            else:
                items.append({"name": name, "type": "BOOLEAN", "value": bool(value)})
        elif isinstance(value, str):
            items.append({"name": name, "type": "STRING", "subtype": 0, "value": value})
        elif isinstance(value, (list, tuple)):
            subtype = "FLOAT" if kind == "FLOAT" or any(isinstance(v, float) for v in value) else "INT"
            items.append({"name": name, "type": "ARRAY", "subtype": subtype, "value": list(value)})
        elif kind == "FLOAT" or isinstance(value, float):
            items.append({"name": name, "type": "FLOAT", "value": float(value)})
        elif isinstance(value, int):
            items.append({"name": name, "type": "INT", "value": int(value)})
        else:
            raise FavoritesImportError(T("{operator}: unsupported value for {name}", operator=c_idname, name=name))
    return {"type": "GROUP", "items": items}


def _check_text(value, limit, what):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what=what))
    return value


def _normalize_item(raw):
    if not isinstance(raw, dict):
        raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="item"))
    type_name = raw.get("type")
    if type_name not in TYPE_IDS:
        raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="type"))
    item = {"type": type_name, "ui_name": _check_text(raw.get("ui_name", ""), NAME_MAX, "ui_name")}
    if type_name == "OPERATOR":
        operator = raw.get("op_idname") or raw.get("operator") or ""
        c_idname = _operator_c_idname(_check_text(operator, NAME_MAX, "op_idname"))
        properties = raw.get("properties")
        if isinstance(properties, dict) and properties.get("type") != "GROUP":
            properties = _typed_from_rna(c_idname, properties) if properties else None
        opcontext = raw.get("opcontext", 0)
        if type(opcontext) is not int or opcontext not in nb.OP_CONTEXT:
            raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="opcontext"))
        item.update(op_idname=_check_text(c_idname, NAME_MAX, "op_idname"),
                    op_prop_enum=_check_text(raw.get("op_prop_enum", "") or "", NAME_MAX, "op_prop_enum"),
                    opcontext=opcontext, properties=properties or None)
    elif type_name == "MENU":
        item["mt_idname"] = _check_text(raw.get("mt_idname", ""), NAME_MAX, "mt_idname")
    elif type_name == "PROPERTY":
        index = raw.get("prop_index", 0)
        if type(index) is not int:
            raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="prop_index"))
        item.update(context_data_path=_check_text(raw.get("context_data_path", ""), DATA_PATH_MAX, "context_data_path"),
                    prop_id=_check_text(raw.get("prop_id", ""), NAME_MAX, "prop_id"), prop_index=index)
    return item


def normalize_favorites(raw):
    """Validate a file's quick_favorites section; returns a clean list of menus."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_MENUS:
        raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="menus"))
    menus, seen, total = [], set(), 0
    for menu in raw:
        if not isinstance(menu, dict) or type(menu.get("space_type")) is not int or not 0 <= menu["space_type"] < 128:
            raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="space_type"))
        key = (menu["space_type"], _check_text(menu.get("context", ""), NAME_MAX, "context"))
        if key in seen:
            raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="duplicate menu"))
        seen.add(key)
        items = menu.get("items", [])
        if not isinstance(items, list):
            raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="items"))
        total += len(items)
        if total > MAX_ITEMS:
            raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="too many items"))
        menus.append({"space_type": key[0], "context": key[1], "items": [_normalize_item(item) for item in items]})
    return menus


# ---------------------------------------------------------------------------
# Python-owned copies of Blender's structs (only linked in while saving)
# ---------------------------------------------------------------------------


class _Arena:
    def __init__(self):
        self.keep = []

    def new(self, cls):
        obj = cls()
        self.keep.append(obj)
        return obj

    def buffer(self, data):
        buf = ctypes.create_string_buffer(data, len(data))
        self.keep.append(buf)
        return buf


def _set_text(obj, field, text, size):
    data = text.encode("utf-8")[: size - 1]
    setattr(obj, field, data + b"\0" * (size - len(data)))


def _link(listbase, structs):
    previous = None
    for obj in structs:
        obj.prev = ctypes.addressof(previous) if previous is not None else None
        obj.next = None
        if previous is not None:
            previous.next = ctypes.addressof(obj)
        previous = obj
    listbase.first = ctypes.addressof(structs[0]) if structs else None
    listbase.last = ctypes.addressof(structs[-1]) if structs else None


def _build_idproperty(arena, data, name=""):
    prop = arena.new(nb._idproperty_cls())
    _set_text(prop, "name", name, 64)
    kind = data.get("type")
    value = data.get("value")
    if kind == "GROUP":
        prop.type = nb.IDP_GROUP
        children = [_build_idproperty(arena, child, child.get("name", "")) for child in data.get("items", [])]
        _link(prop.data.group, children)
        prop.len = len(children)
    elif kind == "STRING":
        raw = str(value or "").encode("utf-8") + b"\0"
        buf = arena.buffer(raw)
        prop.type = nb.IDP_STRING
        prop.subtype = int(data.get("subtype", 0) or 0)
        prop.data.pointer = ctypes.addressof(buf)
        prop.len = prop.totallen = len(raw)
    elif kind in ("INT", "BOOLEAN"):
        prop.type = IDP_IDS[kind]
        prop.data.val = int(bool(value)) if kind == "BOOLEAN" else int(value)
    elif kind == "FLOAT":
        prop.type = nb.IDP_FLOAT
        prop.data.val = struct.unpack("i", struct.pack("f", float(value)))[0]
    elif kind == "DOUBLE":
        prop.type = nb.IDP_DOUBLE
        low, high = struct.unpack("ii", struct.pack("d", float(value)))
        prop.data.val, prop.data.val2 = low, high
    elif kind == "ARRAY":
        subtype = IDP_IDS.get(data.get("subtype"))
        if subtype not in ARRAY_CTYPES or not isinstance(value, list):
            raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what="array"))
        array = (ARRAY_CTYPES[subtype] * max(1, len(value)))(*value)
        arena.keep.append(array)
        prop.type = nb.IDP_ARRAY
        prop.subtype = subtype
        prop.data.pointer = ctypes.addressof(array)
        prop.len = prop.totallen = len(value)
    else:
        raise FavoritesImportError(T("Quick Favorites data is invalid: {what}", what=str(kind)))
    return prop


def _build_item(arena, item):
    kind = TYPE_IDS[item["type"]]
    if kind == nb.USER_MENU_TYPE_OPERATOR:
        obj = arena.new(nb.BUserMenuItem_Op)
        _set_text(obj, "op_idname", item["op_idname"], 64)
        _set_text(obj, "op_prop_enum", item["op_prop_enum"], 64)
        obj.opcontext = item["opcontext"]
        if item.get("properties"):
            obj.prop = ctypes.addressof(_build_idproperty(arena, item["properties"]))
        base = obj.item
    elif kind == nb.USER_MENU_TYPE_MENU:
        obj = arena.new(nb.BUserMenuItem_Menu)
        _set_text(obj, "mt_idname", item["mt_idname"], 64)
        base = obj.item
    elif kind == nb.USER_MENU_TYPE_PROP:
        obj = arena.new(nb.BUserMenuItem_Prop)
        _set_text(obj, "context_data_path", item["context_data_path"], 256)
        _set_text(obj, "prop_id", item["prop_id"], 64)
        obj.prop_index = item["prop_index"]
        base = obj.item
    else:
        obj = arena.new(nb.BUserMenuItem)
        base = obj
    base.type = kind
    _set_text(base, "ui_name", item["ui_name"], 64)
    return base


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _backup_preferences():
    config_dir = Path(bpy.utils.user_resource("CONFIG"))
    pref_path = config_dir / "userpref.blend"
    if not pref_path.exists():
        return ""
    backup_dir = config_dir / "qclick2_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = backup_dir / f"{BACKUP_PREFIX}{stamp}.blend"
    counter = 1
    while backup.exists():
        backup = backup_dir / f"{BACKUP_PREFIX}{stamp}-{counter}.blend"
        counter += 1
    shutil.copy2(pref_path, backup)
    try:  # keep the newest imports' backups only

        ours = sorted(backup_dir.glob(BACKUP_PREFIX + "*.blend"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in ours[nb.BACKUP_KEEP:]:
            stale.unlink()
    except OSError:
        pass
    return str(backup)


def _write_preferences_with(menus, replace_all):
    """Save userpref.blend as if Blender's lists held `menus`; memory is restored after."""
    userdef = nb._userdef()
    existing = {(int(menu.space_type), nb._decode_cstr(menu.context)): (address, menu)
                for address, menu in nb._all_native_menus()}
    arena = _Arena()
    restore_items = []   # (menu, first, last)
    old_menus_first = nb._ptr(userdef.user_menus.first)
    old_menus_last = nb._ptr(userdef.user_menus.last)
    old_last_next = None
    new_menus = []
    wanted = {(menu["space_type"], menu["context"]) for menu in menus}

    for menu in menus:
        items = [_build_item(arena, item) for item in menu["items"]]
        key = (menu["space_type"], menu["context"])
        if key in existing:
            _address, native = existing[key]
            restore_items.append((native, nb._ptr(native.items.first), nb._ptr(native.items.last)))
            _link(native.items, items)
        elif items:
            new_menu = arena.new(nb.BUserMenu)
            new_menu.space_type = menu["space_type"]
            _set_text(new_menu, "context", menu["context"], 64)
            _link(new_menu.items, items)
            new_menus.append(new_menu)
    if replace_all:
        for key, (_address, native) in existing.items():
            if key not in wanted:
                restore_items.append((native, nb._ptr(native.items.first), nb._ptr(native.items.last)))
                native.items.first = native.items.last = None

    try:
        if new_menus:
            _link_tail(userdef.user_menus, new_menus)
            if old_menus_last:
                old_last_next = ctypes.cast(old_menus_last, ctypes.POINTER(nb.BUserMenu)).contents
                old_last_next.next = ctypes.addressof(new_menus[0])
        bpy.ops.wm.save_userpref()
    finally:
        if old_last_next is not None:
            old_last_next.next = None
        userdef.user_menus.first = old_menus_first or None
        userdef.user_menus.last = old_menus_last or None
        for native, first, last in restore_items:
            native.items.first = first or None
            native.items.last = last or None
    return arena


def _link_tail(listbase, structs):
    """Append Python-owned menus after Blender's last menu (undone after saving)."""
    tail = nb._ptr(listbase.last)
    previous = None
    for obj in structs:
        obj.prev = ctypes.addressof(previous) if previous is not None else (tail or None)
        obj.next = None
        if previous is not None:
            previous.next = ctypes.addressof(obj)
        previous = obj
    if not tail:
        listbase.first = ctypes.addressof(structs[0])
    listbase.last = ctypes.addressof(structs[-1])


def _reload_preferences():
    try:
        window = bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window):
            bpy.ops.wm.read_userpref()
    except Exception:
        try:
            bpy.ops.wm.read_userpref()
        except Exception as exc:
            print("Q&Click: reload preferences failed:", exc)
    nb._DETACHED_ITEM_ADDRS.clear()
    nb._invalidate_native_cache()
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:
        pass
    return None


def import_favorites(menus, replace_all=False):
    """Make Blender's Quick Favorites match `menus`; returns (ok, message)."""
    if not nb._native_bridge_can_write():
        return (False, nb._native_bridge_status_text())
    try:
        menus = normalize_favorites(menus)
    except FavoritesImportError as exc:
        return (False, str(exc))
    if menus is None:
        return (True, "")
    try:
        backup = _backup_preferences()
        _write_preferences_with(menus, replace_all)
    except Exception as exc:
        nb._invalidate_native_cache()
        return (False, T("Quick Favorites were not imported: {error}", error=exc))
    # Re-read once the operator has finished, so Blender owns every item.
    bpy.app.timers.register(_reload_preferences, first_interval=0.05)
    message = T("Imported {count} Quick Favorite(s)", count=favorites_count(menus))
    if backup:
        message += T("; preferences were backed up as {name}", name=Path(backup).name)
    return (True, message)
