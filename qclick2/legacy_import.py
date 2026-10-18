# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)
"""Read-only 1.x preview and explicit, backed-up profile import. Never import 1.x code."""

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator, Panel

from . import favorites_io, native_bridge as bridge, persistence, runtime, shortcuts
from .i18n import T

_REGISTERED = False
_EPOCH = 0
_ACTIVE_DIALOG = None
_SOURCE_CHOICES = [("CUSTOM", "Choose or paste a path", "")]
MAX_BYTES = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*")


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(T("The JSON has a duplicate key: {key}", key=key))
        result[key] = value
    return result


def read_source(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError(T("Choose a 1.0 JSON settings file smaller than 8 MB"))
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError(T("The settings file is too large"))
    payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object_pairs,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError(T("Invalid number: {value}", value=value))))
    return path, raw, payload


def _name(value, fallback):
    value = str(value or fallback).strip()
    if not value or len(value) > 96:
        raise ValueError(T("A name is empty or longer than 96 characters; check the source file first"))
    return value


def _properties(operator_id, values):
    if not isinstance(values, dict):
        raise ValueError(T("Invalid operator parameters: {id}", id=operator_id))
    values = shortcuts.encode_value(values)
    rna = bridge._operator_rna_type(operator_id)
    if rna:
        for key, value in list(values.items()):
            prop = rna.properties.get(key)
            if prop and prop.type == "ENUM" and prop.is_enum_flag and isinstance(value, list):
                if any(not isinstance(item, str) for item in value):
                    raise ValueError(T("Invalid multi-choice enum parameter: {id}", id=operator_id))
                values[key] = {shortcuts.SET_TAG: sorted(set(value))}
    return values


def convert_action(raw, warnings, *, manual=False):
    if not isinstance(raw, dict) or raw.get("kind", "ACTION") != "ACTION":
        raise ValueError(T("Unrecognised action"))
    kind = raw.get("action_type", "OPERATOR")
    if kind not in {"OPERATOR", "PROPERTY", "MENU"}:
        raise ValueError(T("Unsupported action type: {kind}", kind=kind))
    source = copy.deepcopy(raw)
    source.pop("manual_command", None)
    source.pop("frozen_parameters", None)
    # Old aliases are not evidence for enum/boolean inference.
    source["label"] = source.get("_default_label") or source.get("label") or source.get("operator") or source.get("menu") or T("Action")
    if kind == "OPERATOR":
        operator_id = str(source.get("operator", ""))
        if not _IDENTIFIER.fullmatch(operator_id) or operator_id.startswith(("qfav.", "qclick2.")):
            raise ValueError(T("Cannot import a control operator or an invalid ID: {id}", id=operator_id))
        source["properties"] = _properties(operator_id, source.get("properties", {}))
        if manual:
            if source["properties"]:
                raise ValueError(T("A manual source has fixed parameters, which this format does not support yet"))
        if bridge._operator_rna_type(operator_id) is None:
            warnings.add(T("Operator not installed: {id}", id=operator_id))
        if operator_id in {"wm.call_menu", "wm.call_menu_pie"}:
            menu_id = source["properties"].get("name")
            if isinstance(menu_id, str) and not hasattr(bpy.types, menu_id):
                warnings.add(T("Menu not installed: {id}", id=menu_id))
    elif kind == "MENU":
        if not isinstance(source.get("menu"), str) or not source["menu"]:
            raise ValueError(T("Incomplete menu ID"))
        if not hasattr(bpy.types, source["menu"]):
            warnings.add(T("Menu not installed: {id}", id=source["menu"]))
    else:
        if not isinstance(source.get("data_path"), str) or not isinstance(source.get("property"), str) or not source["property"]:
            raise ValueError(T("Incomplete property action data"))
        source.setdefault("source_space_type", 1)
        if not source["source_space_type"]:
            source["source_space_type"] = 1
    result = bridge._stored_action_entry(source)
    if result.get("enum_property"):
        raise ValueError(T("The operator's enum menu cannot be converted safely yet; install the tool or pick a specific item in 1.0: {id}", id=result["operator"]))
    if result["opcontext"] not in bridge.OP_CONTEXT:
        raise ValueError(T("Unsupported operator execution context"))
    if kind == "OPERATOR":
        result["frozen_parameters"] = True
        if manual:
            # Freeze the old canonical defaults before marking this manual source.
            result["manual_command"] = True
    result["label"] = _name(raw.get("label"), result["label"])
    if raw.get("hotkey_id"):
        result["shortcut_id"] = "hotkey_" + str(raw["hotkey_id"])
    return result


def _identity_key(text, warnings):
    value = json.loads(text)
    if not isinstance(value, list) or not value:
        raise ValueError(T("Invalid display name or last-action identifier"))
    if value[0] == "OPERATOR" and len(value) == 3:
        raw = {"operator": value[1], "properties": json.loads(value[2]), "label": value[1]}
    elif value[0] == "MENU" and len(value) == 2:
        raw = {"action_type": "MENU", "menu": value[1], "label": value[1]}
    elif value[0] == "PROPERTY" and len(value) == 4:
        raw = {"action_type": "PROPERTY", "data_path": value[1], "property": value[2], "index": value[3]}
    else:
        raise ValueError(T("Unsupported display name or last-action identifier"))
    return persistence.alias_key(convert_action(raw, warnings))


def convert_profile(raw, warnings):
    if not isinstance(raw, dict) or raw.get("version", 1) != 1:
        raise ValueError(T("Unsupported 1.0 context version"))
    layers, groups = raw.get("layers"), raw.get("groups", {})
    if not isinstance(layers, dict) or "root" not in layers or not isinstance(groups, dict):
        raise ValueError(T("Main wheel or group data is missing"))
    profile = persistence._empty_profile()
    folders = {key: "folder_" + key for key in layers if isinstance(key, str) and key != "root"}
    group_ids = {key: "group_" + key for key in groups if isinstance(key, str)}
    if len(folders) != len(layers) - 1 or len(group_ids) != len(groups):
        raise ValueError(T("Invalid wheel identifier"))

    def entries(values, count, group=False):
        if not isinstance(values, list) or len(values) != count:
            raise ValueError(T("A wheel needs 8 slots and a group 4 items"))
        result = []
        for entry in values:
            if entry is None:
                result.append(None)
                continue
            if not isinstance(entry, dict):
                raise ValueError(T("Invalid slot format"))
            kind = entry.get("kind", "ACTION")
            if kind == "ACTION":
                result.append(convert_action(entry, warnings))
            elif kind in {"FOLDER", "GROUP"} and not group:
                old_id = entry.get("layer_id") if kind == "FOLDER" else entry.get("group_id")
                mapping = folders if kind == "FOLDER" else group_ids
                if old_id not in mapping:
                    raise ValueError(T("A slot links to a sub-wheel or group that does not exist"))
                result.append({"kind": kind, "layer_id": mapping[old_id], "label": _name(entry.get("label"), kind)})
            else:
                raise ValueError(T("A group cannot contain sub-wheels or other groups"))
        return result

    profile["slots"] = entries(layers["root"].get("slots"), 8)
    for key, target in folders.items():
        layer = layers[key]
        profile["folders"][target] = {"label": _name(layer.get("name"), T("Sub-wheel")), "slots": entries(layer.get("slots"), 8)}
    for key, target in group_ids.items():
        group = groups[key]
        profile["groups"][target] = {"label": _name(group.get("name"), T("Group")), "slots": entries(group.get("items"), 4, True)}
    actions, hotkeys = raw.get("actions", []), raw.get("hotkeys", [])
    if not isinstance(actions, list) or not isinstance(hotkeys, list):
        raise ValueError(T("Invalid source list format"))
    for index, source in enumerate(actions):
        action = convert_action(source, warnings, manual=True)
        if action["action_type"] != "OPERATOR":
            raise ValueError(T("A manual source must be an operator"))
        key = "command_" + str(source.get("id") or index)
        if key in profile["commands"]:
            raise ValueError(T("Duplicate manual source identifier"))
        profile["commands"][key] = {"label": action["label"], "action": action}
    for index, source in enumerate(hotkeys):
        if not isinstance(source, dict) or not isinstance(source.get("shortcut"), dict):
            raise ValueError(T("Incomplete recorded shortcut data"))
        shortcut = source["shortcut"]
        if not isinstance(shortcut.get("type"), str) or not shortcut["type"] or shortcut.get("value", "PRESS") not in {"PRESS", "CLICK", "ANY"}:
            raise ValueError(T("Unsupported recorded shortcut event"))
        action = convert_action(source.get("action"), warnings)
        if action["action_type"] != "OPERATOR":
            raise ValueError(T("A recorded shortcut has no operator"))
        key = "hotkey_" + str(source.get("id") or index)
        if key in profile["shortcuts"]:
            raise ValueError(T("Duplicate recorded shortcut identifier"))
        profile["shortcuts"][key] = {"label": _name(source.get("label"), action["label"]), "action": action,
                                    "shortcut": {key: value for key, value in shortcut.items() if key in {"type", "ctrl", "shift", "alt", "oskey"}},
                                    "keymap": str(source.get("resolved_keymap", ""))}
    aliases = raw.get("action_aliases", {})
    exclusions = raw.get("last_action_exclusions", [])
    if not isinstance(aliases, dict) or not isinstance(exclusions, list):
        raise ValueError(T("Incomplete display name or last-action settings"))
    for key, label in aliases.items():
        new_key = _identity_key(key, warnings)
        name = _name(label, "")
        if new_key in profile["aliases"] and profile["aliases"][new_key] != name:
            raise ValueError(T("The same action has conflicting display names"))
        profile["aliases"][new_key] = name
    profile["repeat_exclusions"] = sorted({_identity_key(key, warnings) for key in exclusions})
    # Recover rename links by recorded source ID, but keep detached snapshots valid.
    for layer in [profile, *profile["folders"].values(), *profile["groups"].values()]:
        for action in layer["slots"]:
            if action and action.get("shortcut_id") not in profile["shortcuts"]:
                action.pop("shortcut_id", None)
    persistence._validate_profile(profile)
    return profile


def convert_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError(T("Not a 1.0 JSON settings file"))
    if "format" in payload:
        if payload.get("format") != "qfav_pie_clean_layout" or type(payload.get("schema_version")) is not int or payload["schema_version"] not in {1, 2}:
            raise ValueError(T("Not a supported 1.0 settings format or version"))
        root = payload.get("state")
    else:
        root = payload
    if not isinstance(root, dict):
        raise ValueError(T("The 1.0 settings content is missing"))
    if "profiles" in root:
        if root.get("version", 2) != 2 or not isinstance(root["profiles"], dict):
            raise ValueError(T("Unsupported context packaging"))
        profiles = root["profiles"]
    elif "layers" in root:
        profiles = {"VIEW_3D|mesh_edit": root}
    else:
        raise ValueError(T("The 1.0 wheel layout is missing"))
    converted, skipped, warnings = {}, [], set()
    for key, profile in profiles.items():
        if not isinstance(key, str):
            raise ValueError(T("The context name is invalid"))
        if not key.startswith("VIEW_3D|") or key.partition("|")[2] not in persistence.MODE_LABELS:
            skipped.append(key)
            continue
        try:
            converted[key] = convert_profile(profile, warnings)
        except Exception as exc:
            raise ValueError(T("{name}: {error}", name=key, error=exc)) from None
    if root.get("legacy_template"):
        warnings.add(T("The legacy_template fallback layout is not imported"))
    return converted, skipped, sorted(warnings)


def has_content(profile):
    return any(profile["slots"]) or any(profile[key] for key in ("folders", "groups", "commands", "shortcuts", "aliases", "repeat_exclusions"))


def preview(path, mode="SKIP"):
    if mode not in {"SKIP", "REPLACE"}:
        raise ValueError(T("Invalid import option"))
    source, raw, payload = read_source(path)
    if source == persistence._state_path().resolve():
        raise ValueError(T("The source cannot be the current settings file itself"))
    incoming, unsupported, warnings = convert_payload(payload)
    existing = persistence._load_profiles()
    if persistence.load_error():
        raise ValueError(persistence.load_error())
    conflicts = [key for key in incoming if key in existing and has_content(existing[key])]
    chosen = {key: value for key, value in incoming.items() if mode == "REPLACE" or key not in conflicts}
    unchanged = [key for key in chosen if existing.get(key) == chosen[key]]
    chosen = {key: value for key, value in chosen.items() if key not in unchanged}
    counts = {name: sum(len(p[name]) for p in chosen.values()) for name in ("folders", "groups", "shortcuts", "commands")}
    return dict(path=str(source), raw=raw, digest=hashlib.sha256(raw).hexdigest(), stamp=persistence._file_stamp(),
                existing=existing, chosen=chosen, conflicts=conflicts, unsupported=unsupported, warnings=warnings,
                unchanged=unchanged, counts=counts, mode=mode)


def commit(plan):
    """No native preference writes; source and destination must still match preview."""
    source, raw, _payload = read_source(plan["path"])
    if hashlib.sha256(raw).hexdigest() != plan["digest"]:
        raise ValueError(T("The 1.0 file changed; preview again"))
    existing = persistence._load_profiles()
    if persistence.load_error() or persistence._file_stamp() != plan["stamp"] or existing != plan["existing"]:
        raise ValueError(T("The current settings changed; preview again"))
    if not plan["chosen"]:
        raise ValueError(T("Nothing to import"))
    merged = copy.deepcopy(existing)
    merged.update(copy.deepcopy(plan["chosen"]))
    persistence._normalize_profiles(merged)
    destination = persistence._state_path()
    token = uuid.uuid4().hex
    backup = destination.with_name("slots.before-1x-import-" + token + ".json")
    source_backup = destination.with_name("source-1x-import-" + token + ".json")
    old = destination.read_bytes() if destination.exists() else json.dumps({"format": persistence.STATE_FORMAT, "version": persistence.STATE_VERSION, "profiles": {}}).encode("utf-8")
    with backup.open("xb") as stream:
        stream.write(old)
    with source_backup.open("xb") as stream:
        stream.write(raw)
    ok, message = persistence._save_profiles(merged)
    if not ok:
        raise ValueError(message)
    from . import dispatch
    for key in plan["chosen"]:
        dispatch._LAST_ACTION_BY_CONTEXT.pop(key, None)
    shortcuts.status(T("Imported {count} mode(s); settings were backed up first", count=len(plan["chosen"])))
    return backup


def detected_sources():
    config = Path(bpy.utils.user_resource("CONFIG"))
    paths = [config / "qfav_pie_clean" / "layout_state.json"]
    parent = config.parent.parent
    if config.name.lower() == "config":
        paths.extend(sorted(parent.glob("*/config/qfav_pie_clean/layout_state.json")))
    return list(dict.fromkeys(str(path.resolve()) for path in paths if path.is_file()))


def _source_items(self, context):
    # Blender calls this with OperatorProperties, not the modal Python instance.
    # Keep strings alive for the lifetime of the displayed enum.
    return _SOURCE_CHOICES


def _choose_source(self, context):
    if self.source_version != "CUSTOM":
        self.filepath = self.source_version


class QCLICK2_OT_import_legacy(Operator):
    bl_idname = "qclick2.import_legacy"
    bl_label = "Import Q&Click 1.0 Settings"
    bl_options = {"INTERNAL"}
    filepath: StringProperty(name="1.0 Settings File", subtype="FILE_PATH", options={"SKIP_SAVE"})
    source_version: EnumProperty(name="Detected Source", items=_source_items, update=_choose_source, options={"SKIP_SAVE"})
    mode: EnumProperty(name="When a Mode Exists", items=(("SKIP", "Keep current, skip that mode", ""), ("REPLACE", "Replace modes found in the source", "")), default="SKIP", options={"SKIP_SAVE"})
    confirm_replace: BoolProperty(name="I confirm replacing the modes listed in the preview", default=False, options={"SKIP_SAVE"})
    _plan = None
    _error = ""
    _signature = None
    _epoch = None

    def invoke(self, context, event):
        global _ACTIVE_DIALOG, _SOURCE_CHOICES
        if not _REGISTERED or _ACTIVE_DIALOG is not None or context.window is None:
            return {"CANCELLED"}
        paths = detected_sources()
        _SOURCE_CHOICES = [("CUSTOM", T("Choose or paste a path"), "")] + [(path, Path(path).parents[2].name, path) for path in paths]
        if not self.filepath and paths:
            self.filepath = paths[0]
        self._epoch = _EPOCH
        self._window = context.window.as_pointer()
        self._plan = None
        self._signature = None
        self.confirm_replace = False
        self.refresh()
        _ACTIVE_DIALOG = self
        try:
            return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_LARGE, title=T("Import Q&Click 1.0 Settings"))
        except Exception:
            self.close()
            raise

    def refresh(self):
        signature = (self.filepath, self.mode)
        if signature == self._signature:
            return
        self._signature = signature
        self.confirm_replace = False
        try:
            self._plan = preview(self.filepath, self.mode)
            self._error = ""
        except Exception as exc:
            self._plan = None
            self._error = str(exc)

    def check(self, context):
        self.refresh()
        return True

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "source_version", text=T("Detected Source"))
        layout.prop(self, "filepath", text=T("1.0 Settings File"))
        layout.label(text=T("When a Mode Exists"))
        row = layout.row(align=True)
        row.prop_enum(self, "mode", "SKIP", text=T("Keep current, skip that mode"))
        row.prop_enum(self, "mode", "REPLACE", text=T("Replace modes found in the source"))
        if self._error:
            layout.label(text=self._error[:100], icon="ERROR")
            return
        plan = self._plan
        if plan is None:
            return
        counts = plan["counts"]
        layout.label(text=T("Will import {modes} mode(s); sub-wheels {folders}; groups {groups}", modes=len(plan['chosen']), folders=counts['folders'], groups=counts['groups']))
        layout.label(text=T("Recorded shortcuts {shortcuts}; manual commands {commands}", shortcuts=counts['shortcuts'], commands=counts['commands']))
        for key in list(plan["chosen"])[:8]:
            layout.label(text=T("Import: {name}", name=key))
        if plan["conflicts"]:
            layout.label(text=(T("Will replace: {names}") if self.mode == "REPLACE" else T("Skipping existing: {names}")).format(names=", ".join(plan["conflicts"])[:95]), icon="ERROR" if self.mode == "REPLACE" else "INFO")
            if self.mode == "REPLACE":
                layout.prop(self, "confirm_replace", text=T("I confirm replacing the modes listed in the preview"))
        if plan["unsupported"]:
            layout.label(text=T("Not supported yet, left in the original file: {names}", names=", ".join(plan["unsupported"])[:85]), icon="INFO")
        for warning in plan["warnings"][:4]:
            layout.label(text=warning[:100], icon="INFO")
        if len(plan["warnings"]) > 4:
            layout.label(text=T("{count} more dependency or compatibility notes", count=len(plan['warnings']) - 4))
        if not plan["chosen"]:
            layout.label(text=T("Nothing to import (empty, already present or identical)"), icon="INFO")
        layout.label(text=T("Native Blender Quick Favorites are not moved; the old shortcut key and panel settings are not applied"))
        layout.label(text=T("The 1.0 file is not changed; after confirming, both files are backed up before importing"))

    def close(self):
        global _ACTIVE_DIALOG
        if _ACTIVE_DIALOG is self:
            _ACTIVE_DIALOG = None

    def execute(self, context):
        try:
            if not _REGISTERED or self._epoch != _EPOCH or context.window is None or self._window != context.window.as_pointer():
                raise ValueError(T("The context changed; preview again"))
            if self._signature != (self.filepath, self.mode) or self._plan is None:
                raise ValueError(T("The source or options changed; preview again first"))
            if self.mode == "REPLACE" and self._plan["conflicts"] and not self.confirm_replace:
                raise ValueError(T("Replacing existing modes has not been confirmed"))
            commit(self._plan)
        except Exception as exc:
            shortcuts.status(T("Not imported: {error}", error=exc))
            self.report({"WARNING"}, str(exc))
            self.close()
            return {"CANCELLED"}
        self.close()
        return {"FINISHED"}

    def cancel(self, context):
        self.close()
        shortcuts.status(T("Import cancelled; layout unchanged"))


_RESTORE_PATHS = []
_RESTORE_ITEMS = []


def _restore_items(_self, _context):
    # Blender keeps only references to these strings; hold them module-wide.
    return _RESTORE_ITEMS or [("NONE", T("No backups available"), "")]


def _wrap_label(text, width=22):
    text = str(text)
    if all(ord(char) < 0x2E80 for char in text):
        import textwrap
        return textwrap.wrap(text, width * 2) or [""]
    return [text[index:index + width] for index in range(0, len(text), width)] or [""]


class QCLICK2_OT_restore_settings(Operator):
    """Swap a validated backup in for slots.json; the current file is kept."""
    bl_idname = "qclick2.restore_settings"
    bl_label = "Restore Wheel Settings from Backup"
    bl_options = {"INTERNAL"}

    backup: EnumProperty(name="Backup", items=_restore_items, options={"SKIP_SAVE"})

    _invalid = ()

    def invoke(self, context, _event):
        global _RESTORE_PATHS, _RESTORE_ITEMS
        from datetime import datetime
        candidates = persistence.restore_candidates()
        valid = [(path, detail) for path, ok, detail in candidates if ok]
        self._invalid = tuple((path.name, detail) for path, ok, detail in candidates if not ok)
        _RESTORE_PATHS = [str(path) for path, _detail in valid]
        _RESTORE_ITEMS = [
            (str(index), f"{path.name}  ·  {datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M}  ·  {detail}", "")
            for index, (path, detail) in enumerate(valid)
        ]
        if not _RESTORE_ITEMS:
            message = T("No backup passes the checks") + (T("; the backups found cannot be read either") if self._invalid else "")
            self.report({"WARNING"}, message)
            shortcuts.status(message)
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=runtime.DIALOG_WIDTH_LARGE, title=T("Restore Wheel Settings from Backup"))

    def draw(self, _context):
        layout = self.layout
        error = persistence.load_error()
        if error:
            box = layout.box()
            box.alert = True
            box.label(text=T("The current settings cannot be read:"), icon="ERROR")
            for line in _wrap_label(error, 40):
                box.label(text=line)
        layout.prop(self, "backup", text="")
        layout.label(text=T("Only backups that pass every check are listed, newest first."), icon="INFO")
        layout.label(text=T("The current slots.json is first kept as slots.broken-<date>.json, never deleted."))
        for name, detail in self._invalid:
            layout.label(text=T("Skipped {name}: {detail}", name=name, detail=detail[:40]), icon="CANCEL")

    def execute(self, context):
        try:
            path = _RESTORE_PATHS[int(self.backup)]
        except (ValueError, IndexError):
            self.report({"WARNING"}, T("The backup list changed; open it again"))
            return {"CANCELLED"}
        ok, message = persistence.restore_from_backup(path)
        shortcuts.status(message)
        self.report({"INFO"} if ok else {"ERROR"}, message)
        from . import editor
        editor._tag_view3d_redraw()
        return {"FINISHED"} if ok else {"CANCELLED"}


def _default_export_path():
    from datetime import datetime
    folder = Path.home() / "Documents"
    if not folder.is_dir():
        folder = Path.home()
    return str(folder / f"qclick2_settings_{datetime.now():%Y%m%d}.json")


def _file_favorites(filepath):
    """The quick_favorites section of an exported file: (menus or None, error)."""
    try:
        source = Path(filepath)
        if not source.is_file() or source.stat().st_size > persistence.EXPORT_MAX_BYTES:
            return None, ""
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None, ""
        return favorites_io.normalize_favorites(payload.get(favorites_io.FORMAT_KEY)), ""
    except Exception as exc:
        return None, str(exc)


class QCLICK2_OT_export_settings(Operator):
    """Save every mode's wheels, sub-wheels, groups, action library and Blender Quick Favorites to a file"""
    bl_idname = "qclick2.export_settings"
    bl_label = "Export Wheel Settings"
    bl_options = {"INTERNAL"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    check_existing: BoolProperty(default=True, options={"HIDDEN"})
    include_favorites: BoolProperty(name="Include Quick Favorites", default=True)

    def invoke(self, context, _event):
        if not self.filepath:
            self.filepath = _default_export_path()
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        self.layout.prop(self, "include_favorites", text=T("Include Quick Favorites"))

    def execute(self, _context):
        extra, note = {}, ""
        if self.include_favorites:
            menus, error = favorites_io.export_favorites()
            if menus is None:
                note = T("; Quick Favorites could not be read and were left out: {error}", error=error)
            else:
                extra[favorites_io.FORMAT_KEY] = menus
                note = T("; {count} Quick Favorite(s) included", count=favorites_io.favorites_count(menus))
        ok, message = persistence.export_settings(self.filepath, extra=extra)
        if ok:
            message += note
        shortcuts.status(message)
        self.report({"INFO"} if ok else {"ERROR"}, message)
        return {"FINISHED"} if ok else {"CANCELLED"}


class QCLICK2_OT_import_settings(Operator):
    """Load wheels exported from Q&Click; the current settings are backed up first"""
    bl_idname = "qclick2.import_settings"
    bl_label = "Import Q&Click Settings"
    bl_options = {"INTERNAL"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    mode: EnumProperty(
        name="Import Mode",
        items=(("MERGE", "Merge: replace only the modes in the file", "Modes not in the file stay as they are"),
               ("REPLACE", "Replace All", "Swap the whole settings file for the file's contents")),
        default="MERGE", options={"SKIP_SAVE"})
    import_favorites: BoolProperty(name="Import Quick Favorites", default=True, options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, _context):
        layout = self.layout
        layout.prop_enum(self, "mode", "MERGE", text=T("Merge: replace only the modes in the file"))
        layout.prop_enum(self, "mode", "REPLACE", text=T("Replace All"))
        incoming, error = persistence.preview_import(self.filepath) if self.filepath.lower().endswith(".json") else (None, "")
        if incoming is not None:
            layout.label(text=T("The file has {count} mode(s)", count=len(incoming)), icon="CHECKMARK")
            for key in sorted(incoming)[:8]:
                area, _sep, mode = key.partition("|")
                layout.label(text=f"· {persistence.mode_label(mode)} ({persistence.area_label(area)})")
            favorites, favorites_error = _file_favorites(self.filepath)
            if favorites is not None:
                layout.prop(self, "import_favorites",
                            text=T("Import {count} Quick Favorite(s)", count=favorites_io.favorites_count(favorites)))
                if self.import_favorites:
                    layout.label(text=T("Quick Favorites in these menus are replaced"), icon="INFO")
            elif favorites_error:
                box = layout.box()
                box.alert = True
                for line in _wrap_label(T("Quick Favorites in the file are invalid: {error}", error=favorites_error), 28):
                    box.label(text=line)
            else:
                layout.label(text=T("The file has no Quick Favorites"), icon="INFO")
        elif error:
            box = layout.box()
            box.alert = True
            for line in _wrap_label(error, 28):
                box.label(text=line)
        layout.label(text=T("The current settings are backed up before importing;"), icon="INFO")
        layout.label(text=T("use Restore from Backup to undo."))

    def execute(self, _context):
        favorites, favorites_error = _file_favorites(self.filepath) if self.import_favorites else (None, "")
        if favorites_error:
            message = T("Quick Favorites in the file are invalid: {error}", error=favorites_error)
            shortcuts.status(message)
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        ok, message = persistence.import_settings(self.filepath, self.mode)
        if ok and favorites is not None:
            favorites_ok, favorites_message = favorites_io.import_favorites(favorites, replace_all=self.mode == "REPLACE")
            if favorites_message:
                message += T("; ") + favorites_message
            ok = favorites_ok
        shortcuts.status(message)
        self.report({"INFO"} if ok else {"ERROR"}, message)
        from . import editor
        editor._tag_view3d_redraw()
        return {"FINISHED"} if ok else {"CANCELLED"}


# Importing from the private, never-published Q&Click 1.x is kept working but
# hidden from the public release; set True to show its button again.
SHOW_LEGACY_IMPORT = False


class _ImportPanelBase:
    bl_label = "Settings Import and Export"
    bl_region_type = "UI"
    bl_category = "Q&Click"
    bl_order = 20
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        error = persistence.load_error()
        if error:
            # Every wheel is empty and saving is blocked until this is fixed,
            # so the way out sits at the top, not behind the import button.
            box = layout.box()
            box.alert = True
            box.label(text=T("Wheel settings cannot be read"), icon="ERROR")
            for line in _wrap_label(error):
                box.label(text=line)
            box.operator("qclick2.restore_settings", text=T("Restore Wheel Settings from Backup"), icon="RECOVER_LAST")
            box.operator("qclick2.import_settings", text=T("Import Previously Exported Settings"), icon="IMPORT")
        column = layout.column(align=True)
        column.operator("qclick2.export_settings", text=T("Export Wheel Settings"), icon="EXPORT")
        column.operator("qclick2.import_settings", text=T("Import Q&Click Settings"), icon="IMPORT")
        if SHOW_LEGACY_IMPORT:
            layout.operator("qclick2.import_legacy", text=T("Import Q&Click 1.0 Settings"), icon="IMPORT")
        if not error:
            layout.operator("qclick2.restore_settings", text=T("Restore Wheel Settings from Backup"), icon="RECOVER_LAST")


class QCLICK2_PT_import_legacy(_ImportPanelBase, Panel):
    bl_idname = "QCLICK2_PT_import_legacy"
    bl_space_type = "VIEW_3D"


class QCLICK2_PT_import_legacy_image(_ImportPanelBase, Panel):
    bl_idname = "QCLICK2_PT_import_legacy_image"
    bl_space_type = "IMAGE_EDITOR"


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


CLASSES = (QCLICK2_OT_import_legacy, QCLICK2_OT_restore_settings, QCLICK2_OT_export_settings, QCLICK2_OT_import_settings, QCLICK2_PT_import_legacy, QCLICK2_PT_import_legacy_image)
