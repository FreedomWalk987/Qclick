# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Q&Click own wheel persistence.

Wheel layouts are stored per editor mode (one profile per mode, like native
Quick Favorites): ``VIEW_3D|objectmode``, ``VIEW_3D|mesh_edit`` and so on.
Each profile holds 8 main-wheel slots and a library of linked 8-slot sub-wheels.
"""

import copy
from contextlib import contextmanager
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import bpy

from .i18n import T

STATE_FORMAT = "qclick2_slots"
STATE_VERSION = 9
SLOT_COUNT = 8
GROUP_SLOT_COUNT = 4
ROOT = "root"
LIBRARIES = {"FOLDER": "folders", "GROUP": "groups"}

_PROFILES_CACHE = None
_CACHE_STAMP = None
_LOAD_ERROR = ""


def _file_stamp():
    path = _state_path()
    if not path.exists():
        return (str(path), None)
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def load_error():
    return _LOAD_ERROR

MODE_LABELS = {
    "objectmode": "Object Mode",
    "mesh_edit": "Edit Mesh",
    "curve_edit": "Edit Curve",
    "surface_edit": "Edit Surface",
    "text_edit": "Edit Text",
    "armature_edit": "Edit Armature",
    "mball_edit": "Edit Metaball",
    "lattice_edit": "Edit Lattice",
    "curves_edit": "Edit Curves",
    "grease_pencil_edit": "Edit Grease Pencil",
    "posemode": "Pose Mode",
    "sculpt_mode": "Sculpt Mode",
    "weightpaint": "Weight Paint",
    "vertexpaint": "Vertex Paint",
    "imagepaint": "Texture Paint",
    "particlemode": "Particle Edit",
}


# Editors the wheel and its sidebar run in.  Keys stay "<area type>|<mode>",
# so profiles saved by earlier builds (all of them VIEW_3D) keep working.
SUPPORTED_AREAS = ("VIEW_3D", "IMAGE_EDITOR")

AREA_LABELS = {
    "VIEW_3D": "3D Viewport",
    "IMAGE_EDITOR": "UV / Image Editor",
}


def area_type(context):
    area = getattr(context, "area", None)
    area_name = str(getattr(area, "type", "") or "")
    return area_name if area_name in SUPPORTED_AREAS else "VIEW_3D"


def _profile_key(context):
    from .native_bridge import _native_mode

    return f"{area_type(context)}|{_native_mode(context)}"


def profile_label(context):
    """Readable label for the current wheel profile, e.g. Edit Mesh."""
    from .native_bridge import _native_mode

    mode = _native_mode(context)
    label = mode_label(mode)
    area_name = area_type(context)
    if area_name != "VIEW_3D":
        return f"{area_label(area_name)} · {label}"
    return label


def mode_label(mode):
    return T(MODE_LABELS.get(mode, mode))


def area_label(area_name):
    return T(AREA_LABELS.get(area_name, area_name))


def _state_path():
    config_dir = Path(bpy.utils.user_resource("CONFIG", path="qclick2", create=True))
    return config_dir / "slots.json"


def _empty_slots(count=SLOT_COUNT):
    return [None for _ in range(count)]


def _normalize_profiles(raw):
    for key, profile in raw.items():
        if not isinstance(key, str):
            raise ValueError(T("The context name is invalid"))
        _validate_profile(profile)
    return copy.deepcopy(raw)


def _empty_profile():
    return {"slots": _empty_slots(), "folders": {}, "groups": {}, "shortcuts": {}, "commands": {}, "repeat_exclusions": [], "aliases": {}}


def _validate_action(entry):
    """Validate stored shapes without requiring third-party tools to be enabled."""
    action_type = entry.get("action_type", "OPERATOR")
    if action_type not in {"OPERATOR", "MENU", "PROPERTY"}:
        raise ValueError(T("Unsupported action type"))
    if "label" in entry and not isinstance(entry["label"], str):
        raise ValueError(T("Invalid action name data"))
    if not isinstance(entry.get("properties", {}), dict):
        raise ValueError(T("Invalid tool parameter data"))
    for field in ("index", "source_space_type", "opcontext"):
        if field in entry and type(entry[field]) is not int:
            raise ValueError(T("Invalid action value data"))
    if entry.get("opcontext", 0) not in range(12):
        raise ValueError(T("Invalid tool execution context"))
    field = {"OPERATOR": "operator", "MENU": "menu", "PROPERTY": "property"}[action_type]
    if not isinstance(entry.get(field), str) or not entry[field].strip():
        raise ValueError(T("Incomplete action identifier"))
    if action_type == "PROPERTY" and not isinstance(entry.get("data_path", ""), str):
        raise ValueError(T("Invalid property path data"))


def _validate_profile(profile):
    if not isinstance(profile, dict) or not isinstance(profile.get("folders"), dict):
        raise ValueError(T("Incomplete sub-wheel data"))
    folders = profile["folders"]
    aliases = profile.get("aliases")
    if not isinstance(aliases, dict) or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip() or len(value) > 96 for key, value in aliases.items()):
        raise ValueError(T("Incomplete display name settings"))
    exclusions = profile.get("repeat_exclusions")
    if not isinstance(exclusions, list) or any(not isinstance(key, str) or not key for key in exclusions) or len(exclusions) != len(set(exclusions)):
        raise ValueError(T("Incomplete last-action exclusion settings"))
    groups = profile.get("groups")
    if not isinstance(groups, dict) or set(folders).intersection(groups):
        raise ValueError(T("Group data is incomplete or has duplicate identifiers"))
    shortcuts = profile.get("shortcuts")
    if not isinstance(shortcuts, dict):
        raise ValueError(T("Incomplete shortcut source data"))
    commands = profile.get("commands")
    if not isinstance(commands, dict):
        raise ValueError(T("Incomplete manual command source data"))
    for key, record in commands.items():
        if not isinstance(key, str) or not key or not isinstance(record, dict):
            raise ValueError(T("Invalid manual command identifier"))
        action = record.get("action")
        if not isinstance(record.get("label"), str) or not record["label"].strip():
            raise ValueError(T("The manual command has no name"))
        if not isinstance(action, dict) or action.get("kind") != "ACTION" or action.get("action_type") != "OPERATOR" or not isinstance(action.get("operator"), str) or not action["operator"] or not isinstance(action.get("properties"), dict) or (action["properties"] and action.get("frozen_parameters") is not True) or action.get("manual_command") is not True:
            raise ValueError(T("Invalid manual command data"))
    for key, record in shortcuts.items():
        if not isinstance(key, str) or not key or not isinstance(record, dict):
            raise ValueError(T("Invalid shortcut source identifier"))
        action = record.get("action")
        shortcut = record.get("shortcut")
        if not isinstance(record.get("label"), str) or not record["label"].strip():
            raise ValueError(T("The shortcut source has no name"))
        if not isinstance(shortcut, dict) or not isinstance(shortcut.get("type"), str) or not shortcut["type"]:
            raise ValueError(T("Incomplete key combination data"))
        if any(type(shortcut.get(modifier, False)) is not bool for modifier in ("ctrl", "shift", "alt", "oskey")):
            raise ValueError(T("Invalid modifier key data"))
        if not isinstance(action, dict) or action.get("kind") != "ACTION" or action.get("action_type") != "OPERATOR" or not isinstance(action.get("operator"), str) or not action["operator"] or not isinstance(action.get("properties"), dict):
            raise ValueError(T("The shortcut source has no tool parameters that can be saved"))
    for folder_id, folder in [*folders.items(), *groups.items()]:
        if not isinstance(folder_id, str) or not folder_id or folder_id == ROOT:
            raise ValueError(T("Invalid sub-wheel identifier"))
        if not isinstance(folder, dict) or not isinstance(folder.get("label"), str) or not folder["label"].strip():
            raise ValueError(T("The sub-wheel has no name"))
    edges = {key: set() for key in folders}
    group_edges = {key: set() for key in groups}
    for layer_id, layer in [(ROOT, profile), *folders.items(), *groups.items()]:
        entries = layer.get("slots")
        is_group = layer_id in groups
        count = GROUP_SLOT_COUNT if is_group else SLOT_COUNT
        if not isinstance(entries, list) or len(entries) != count:
            raise ValueError(T("Incomplete context or slot data"))
        for entry in entries:
            if entry is None:
                continue
            if not isinstance(entry, dict) or entry.get("kind") not in {"ACTION", "FOLDER", "GROUP"}:
                raise ValueError(T("Contains slots this version does not recognise"))
            if entry["kind"] == "ACTION":
                _validate_action(entry)
            if is_group and entry["kind"] == "FOLDER":
                raise ValueError(T("A group can hold actions or groups, but not sub-wheels"))
            if entry["kind"] == "GROUP":
                target = entry.get("layer_id")
                if not isinstance(target, str) or target not in groups:
                    raise ValueError(T("The group no longer exists and cannot be placed"))
                if is_group:
                    group_edges[layer_id].add(target)
            if entry["kind"] == "FOLDER":
                target = entry.get("layer_id")
                if not isinstance(target, str) or target not in folders:
                    raise ValueError(T("The sub-wheel no longer exists and cannot be placed"))
                if layer_id != ROOT:
                    edges[layer_id].add(target)
    # Validate the whole library, including unlinked groups and shared children.
    group_indegree = dict.fromkeys(groups, 0)
    for targets in group_edges.values():
        for child in targets:
            group_indegree[child] += 1
    ready = [key for key, degree in group_indegree.items() if degree == 0]
    checked = 0
    while ready:
        key = ready.pop()
        checked += 1
        for child in group_edges[key]:
            group_indegree[child] -= 1
            if group_indegree[child] == 0:
                ready.append(child)
    if checked != len(groups):
        raise ValueError(T("A group cannot contain itself or form a loop"))
    # Topological validation also covers folders not currently linked at root.
    indegree = dict.fromkeys(folders, 0)
    for targets in edges.values():
        for target in targets:
            indegree[target] += 1
    pending = [key for key, count in indegree.items() if count == 0]
    visited = 0
    while pending:
        key = pending.pop()
        visited += 1
        for target in edges[key]:
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(folders):
        raise ValueError(T("A sub-wheel cannot contain itself or its own sub-wheels"))


def _layer(profile, layer_id):
    if layer_id == ROOT:
        return profile
    for library in LIBRARIES.values():
        if layer_id in profile[library]:
            return profile[library][layer_id]
    raise ValueError(T("The sub-wheel or group no longer exists"))


def profile_slots(profile, layer_id=ROOT):
    slots = copy.deepcopy(_layer(profile, layer_id)["slots"])
    for entry in slots:
        if entry and entry.get("kind") in LIBRARIES:
            entry["label"] = profile[LIBRARIES[entry["kind"]]][entry["layer_id"]]["label"]
    return slots


def _profiles_from_text(text):
    """Parse and fully validate a settings file's contents; raise if unusable."""
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("format") != STATE_FORMAT:
        raise ValueError(T("Settings format does not match"))
    version = payload.get("version", 1)
    if type(version) is not int or version not in (1, 2, 3, 4, 5, 6, 7, 8, STATE_VERSION):
        raise ValueError(T("Unsupported settings version; open it with the matching version"))
    raw = payload.get("profiles") if version >= 2 else {"VIEW_3D|objectmode": payload.get("slots")}
    if not isinstance(raw, dict):
        raise ValueError(T("Settings have no context layouts"))
    if version < 3:
        raw = {key: {"slots": entries, "folders": {}} for key, entries in raw.items()}
    for minimum, field, empty in ((4, "groups", dict), (5, "shortcuts", dict), (6, "commands", dict),
                                  (7, "repeat_exclusions", list), (8, "aliases", dict)):
        if version < minimum:
            for profile in raw.values():
                if not isinstance(profile, dict):
                    raise ValueError(T("Incomplete context data"))
                profile[field] = empty()
    return _normalize_profiles(raw)


def _load_profiles():
    global _PROFILES_CACHE, _CACHE_STAMP, _LOAD_ERROR
    profiles = {}
    try:
        stamp = _file_stamp()
        if _PROFILES_CACHE is not None and stamp == _CACHE_STAMP:
            return copy.deepcopy(_PROFILES_CACHE)
        path = _state_path()
        if path.exists():
            profiles = _profiles_from_text(path.read_text(encoding="utf-8"))
        _CACHE_STAMP = stamp
        _LOAD_ERROR = ""
    except Exception as exc:
        _LOAD_ERROR = T("Settings could not be read; saving is stopped: {error} (restore from a backup in the Settings Import and Export panel)", error=exc)
        _PROFILES_CACHE = None
        return {}
    _PROFILES_CACHE = copy.deepcopy(profiles)
    return profiles


# --- Export / import of Q&Click settings -------------------------------------
#
# The export is the settings file itself (same format and validation), plus a
# few informational keys the loader ignores.  Quick Favorites are not in it:
# they belong to Blender's preferences.  Wheel slots keep their own copy of each
# action, so an imported wheel works even where those favorites do not exist.

EXPORT_MAX_BYTES = 8 * 1024 * 1024


def _write_text_atomic(path, text):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=path.stem + ".", suffix=".tmp",
                                         delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def export_settings(target, version_text=""):
    """Write every mode's wheels to a standalone JSON file."""
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, T("The current settings cannot be read, so they cannot be exported; restore from a backup first"))
    target = Path(target)
    if target.suffix.lower() != ".json":
        target = target.with_name(target.name + ".json")
    try:
        if target.resolve().parent == _state_path().parent.resolve():
            return (False, T("Export to a location outside the settings folder"))
    except OSError:
        pass
    from datetime import datetime
    payload = {
        "format": STATE_FORMAT,
        "version": STATE_VERSION,
        "profiles": profiles,
        "exported_by": ("Q&Click " + str(version_text or "")).strip(),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(target, json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False))
    except Exception as exc:
        return (False, T("Export failed: {error}", error=exc))
    return (True, T("Exported {count} mode(s) to {path}", count=len(profiles), path=target))


def preview_import(source):
    """Validate an exported file without changing anything: (profiles, error)."""
    source = Path(source)
    try:
        if not source.is_file():
            return None, T("File not found")
        if source.stat().st_size > EXPORT_MAX_BYTES:
            return None, T("The file is too large to be Q&Click settings")
        return _profiles_from_text(source.read_text(encoding="utf-8")), ""
    except Exception as exc:
        return None, T("Cannot read: {error}", error=exc)


def import_settings(source, mode="MERGE"):
    """Bring in an exported file.  MERGE replaces only the modes the file has;
REPLACE swaps the whole settings file.  The current file is backed up first as
slots.pre-import-<date>.json.bak, which "restore from backup" can bring back."""
    global _PROFILES_CACHE, _CACHE_STAMP, _LOAD_ERROR
    incoming, error = preview_import(source)
    if incoming is None:
        return (False, error)
    if mode not in {"MERGE", "REPLACE"}:
        return (False, T("Invalid import mode"))
    current = _load_profiles()
    if mode == "MERGE" and _LOAD_ERROR:
        return (False, T("The current settings cannot be read; only Replace All import is possible"))
    merged = copy.deepcopy(incoming) if mode == "REPLACE" else {**current, **copy.deepcopy(incoming)}
    path = _state_path()
    try:
        with _write_lock(path):
            _normalize_profiles(merged)
            backup_name = ""
            if path.exists():
                from datetime import datetime
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = path.with_name(f"slots.pre-import-{stamp}.json.bak")
                counter = 1
                while backup.exists():
                    backup = path.with_name(f"slots.pre-import-{stamp}-{counter}.json.bak")
                    counter += 1
                shutil.copy2(path, backup)
                backup_name = backup.name
                # One backup per import; keep the newest ten of these only.
                older = sorted(path.parent.glob("slots.pre-import-*.json.bak"),
                               key=lambda item: item.stat().st_mtime, reverse=True)[10:]
                for stale in older:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            payload = {"format": STATE_FORMAT, "version": STATE_VERSION, "profiles": merged}
            _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False))
    except _WriteLockBusy:
        return (False, T("Another Blender is saving wheel settings; try again shortly"))
    except Exception as exc:
        return (False, T("Import failed: {error}", error=exc))
    _PROFILES_CACHE = None
    _CACHE_STAMP = None
    _LOAD_ERROR = ""
    _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    message = T("Replaced all settings") if mode == "REPLACE" else T("Imported {count} mode(s)", count=len(incoming))
    if backup_name:
        message += T("; the previous settings were backed up as {name}", name=backup_name)
    return (True, message)


# --- Restore from backup -------------------------------------------------------
#
# A single malformed entry stops all loading and saving, by design: writing on
# top of data that did not validate could destroy what is left of it.  These
# helpers give a way out without hand-editing JSON: pick a backup that passes
# the same validation, keep the broken file aside, and swap the backup in.

def restore_candidates():
    """Backups next to slots.json, newest first: (path, valid, detail)."""
    folder = _state_path().parent
    paths = [folder / "slots.json.bak", *sorted(folder.glob("slots.pre-*.json.bak"))]
    found = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            profiles = _profiles_from_text(path.read_text(encoding="utf-8"))
            found.append((path, True, T("{count} mode(s)", count=len(profiles))))
        except Exception as exc:
            found.append((path, False, str(exc)))
    found.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    return found


def restore_from_backup(backup_path):
    """Replace slots.json with a validated backup; the current file is kept."""
    global _PROFILES_CACHE, _CACHE_STAMP, _LOAD_ERROR
    backup_path = Path(backup_path).resolve()
    path = _state_path()
    if backup_path.parent != path.parent.resolve() or not backup_path.name.endswith(".bak"):
        return (False, T("Only backups in the settings folder can be restored"))
    try:
        with _write_lock(path):
            text = backup_path.read_text(encoding="utf-8")
            _profiles_from_text(text)  # refuse a backup that would not load either
            kept = ""
            if path.exists():
                from datetime import datetime
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                aside = path.with_name(f"slots.broken-{stamp}.json")
                counter = 1
                while aside.exists():
                    aside = path.with_name(f"slots.broken-{stamp}-{counter}.json")
                    counter += 1
                shutil.copy2(path, aside)
                kept = aside.name
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=path.parent, prefix="slots.", suffix=".tmp",
                                                 delete=False) as stream:
                    temp_path = Path(stream.name)
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, path)
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
    except _WriteLockBusy:
        return (False, T("Another Blender is saving wheel settings; try again shortly"))
    except Exception as exc:
        return (False, T("Restore failed: {error}", error=exc))
    _PROFILES_CACHE = None
    _CACHE_STAMP = None
    _LOAD_ERROR = ""
    _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    message = T("Restored wheel settings from {name}", name=backup_path.name)
    if kept:
        message += T("; the previous file was kept as {name}", name=kept)
    return (True, message)


class _WriteLockBusy(Exception):
    """Another process holds the settings write lock right now."""


@contextmanager
def _write_lock(path):
    """Coordinate Blender instances without leaving a stale lock on crashes."""
    with path.with_suffix(".json.lock").open("a+b") as stream:
        stream.seek(0)
        # Only the lock call itself means "busy"; failing to open the lock file
        # is a real I/O problem and must surface as such.
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise _WriteLockBusy() from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _save_profiles(profiles):
    try:
        with _write_lock(_state_path()):
            return _save_profiles_locked(profiles)
    except _WriteLockBusy:
        return (False, T("Another Blender is saving wheel settings; try again shortly"))
    except Exception as exc:
        return (False, T("Cannot save wheel settings: {error}", error=exc))


def _save_profiles_locked(profiles):
    global _PROFILES_CACHE, _CACHE_STAMP
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    payload = {
        "format": STATE_FORMAT,
        "version": STATE_VERSION,
        "profiles": profiles,
    }
    temp_path = None
    try:
        _normalize_profiles(profiles)
        if _file_stamp() != _CACHE_STAMP:
            return (False, T("Settings were changed elsewhere; try again"))
        path = _state_path()
        # Each process owns its staging file; a second Blender instance must
        # never truncate the first writer's in-flight JSON.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix="slots.", suffix=".tmp",
                                         delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=1, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if _file_stamp() != _CACHE_STAMP:
            return (False, T("Settings were changed elsewhere; try again"))
        if path.exists():
            old_version = json.loads(path.read_text(encoding="utf-8")).get("version", 1)
            for threshold, filename in ((3, "slots.pre-subwheels.json.bak"), (4, "slots.pre-groups.json.bak"), (5, "slots.pre-shortcuts.json.bak"), (6, "slots.pre-commands.json.bak"), (7, "slots.pre-repeat-policy.json.bak"), (8, "slots.pre-aliases.json.bak"), (9, "slots.pre-legacy-import.json.bak")):
                migration_backup = path.with_name(filename)
                if old_version < threshold and not migration_backup.exists():
                    shutil.copy2(path, migration_backup)
            shutil.copy2(path, path.with_suffix(".json.bak"))
        if _file_stamp() != _CACHE_STAMP:
            return (False, T("Settings were changed elsewhere; try again"))
        os.replace(temp_path, path)
    except Exception as exc:
        return (False, T("Cannot save wheel settings: {error}", error=exc))
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    _PROFILES_CACHE = copy.deepcopy(profiles)
    try:
        _CACHE_STAMP = _file_stamp()
    except OSError:
        # The replacement succeeded. A later cache stat failure must not
        # misreport a committed write or leave stale cached data in use.
        _PROFILES_CACHE = None
        _CACHE_STAMP = None
    return (True, "")


def load_profile(context):
    return _load_profiles().get(_profile_key(context), _empty_profile())


def repeat_key(action):
    from .native_bridge import _action_identity
    if not isinstance(action, dict) or action.get("kind", "ACTION") != "ACTION" or action.get("action_type", "OPERATOR") not in {"OPERATOR", "PROPERTY"}:
        raise ValueError(T("Only tools and boolean properties can be recorded as the last action"))
    return json.dumps(_action_identity(action), ensure_ascii=False, separators=(",", ":"))


def repeat_allowed_for_key(context_key, action):
    try:
        key = repeat_key(action)
    except (ValueError, TypeError):
        return False
    profile = _load_profiles().get(context_key, _empty_profile())
    return not _LOAD_ERROR and key not in profile["repeat_exclusions"]


def repeat_allowed(context, action):
    return repeat_allowed_for_key(_profile_key(context), action)


def alias_key(action):
    from .native_bridge import _action_identity
    if not isinstance(action, dict) or action.get("kind", "ACTION") != "ACTION" or action.get("action_type", "OPERATOR") not in {"OPERATOR", "PROPERTY", "MENU"}:
        raise ValueError(T("This item does not support a display name"))
    return json.dumps(_action_identity(action), ensure_ascii=False, separators=(",", ":"))


def display_label(action, profile):
    """Resolve presentation only; never replace an execution record's label."""
    if not isinstance(action, dict):
        return ""
    base = str(action.get("label", "") or T("Action"))
    if action.get("kind", "ACTION") != "ACTION" or not profile.get("aliases"):
        return base
    try:
        return profile["aliases"].get(alias_key(action), base)
    except (ValueError, TypeError):
        return base


def presentation_entry(action, profile):
    """Transient display metadata is ignored by execution and dropped on storage."""
    result = copy.deepcopy(action)
    if isinstance(result, dict):
        result["_display_label"] = display_label(action, profile)
    return result


def set_repeat_allowed(context, action, allowed):
    return set_action_settings(context, action, allowed=allowed)


def set_action_settings(context, action, *, allowed=None, alias=None, reset_alias=False, expected_alias=None):
    """Commit a native settings dialog in one atomic write; names never alter tools."""
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    try:
        key = alias_key(action)
        if allowed is not None:
            repeat_key(action)
    except (ValueError, TypeError) as exc:
        return (False, str(exc))
    profile = profiles.setdefault(_profile_key(context), _empty_profile())
    if alias is not None or reset_alias:
        if expected_alias is not None and profile["aliases"].get(key, "") != expected_alias:
            return (False, T("The display name was changed elsewhere; reopen the settings"))
        if reset_alias:
            profile["aliases"].pop(key, None)
        else:
            label = str(alias).strip()
            if not label or len(label) > 96:
                return (False, T("The display name must be 1 to 96 characters"))
            profile["aliases"][key] = label
    exclusions = set(profile["repeat_exclusions"])
    if allowed is True:
        exclusions.discard(key)
    elif allowed is False:
        exclusions.add(key)
    profile["repeat_exclusions"] = sorted(exclusions)
    ok, message = _save_profiles(profiles)
    if ok and allowed is False:
        from . import dispatch
        # Clear only matching history; never replace it with an older tool.
        history_key = _profile_key(context)
        previous = dispatch._LAST_ACTION_BY_CONTEXT.get(history_key)
        if previous is not None and repeat_key(previous) == key:
            dispatch._LAST_ACTION_BY_CONTEXT.pop(history_key, None)
    return (ok, message)


def load_slots(context, layer_id=ROOT):
    """Return one layer's slots: eight radial slots or four group actions."""
    return profile_slots(load_profile(context), layer_id)


def save_slots(context, slots, expected=None, layer_id=ROOT):
    """Persist the current mode's slots atomically; returns (ok, message)."""
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    profile = profiles.setdefault(_profile_key(context), _empty_profile())
    try:
        if expected is not None and profile_slots(profile, layer_id) != list(expected):
            return (False, T("The wheel layout changed; this drag was cancelled"))
        _layer(profile, layer_id)["slots"] = copy.deepcopy(list(slots))
    except ValueError as exc:
        return (False, str(exc))
    return _save_profiles(profiles)


def set_slot(context, index, entry, expected=None, layer_id=ROOT):
    try:
        slots = load_slots(context, layer_id)
    except ValueError as exc:
        return (False, str(exc))
    if not (0 <= int(index) < len(slots)):
        return (False, T("Invalid slot number"))
    slots[int(index)] = entry
    return save_slots(context, slots, expected=expected, layer_id=layer_id)


def move_slot(context, source, target, expected, layer_id=ROOT):
    if not (0 <= source < len(expected) and 0 <= target < len(expected)):
        return (False, T("Invalid slot number"))
    slots = copy.deepcopy(list(expected))
    if source == target or slots[source] is None:
        return (False, T("Layout unchanged"))
    slots[source], slots[target] = slots[target], slots[source]
    return save_slots(context, slots, expected=expected, layer_id=layer_id)


def clear_slot(context, index, layer_id=ROOT):
    return set_slot(context, index, None, layer_id=layer_id)


def reset_slots(context, layer_id=ROOT):
    try:
        slots = load_slots(context, layer_id)
    except ValueError as exc:
        return (False, str(exc))
    return save_slots(context, _empty_slots(len(slots)), layer_id=layer_id)


def folder_entries(context, kind="FOLDER", profile=None):
    profile = load_profile(context) if profile is None else profile
    return [{"kind": kind, "layer_id": key, "label": folder["label"]}
            for key, folder in profile[LIBRARIES[kind]].items()]


def shortcut_entries(context, profile=None):
    profile = load_profile(context) if profile is None else profile
    result = []
    for key, record in profile["shortcuts"].items():
        entry = copy.deepcopy(record["action"])
        entry.update(label=record["label"], shortcut_id=key, recorded_shortcut=copy.deepcopy(record["shortcut"]))
        result.append(entry)
    return result


def save_shortcut(context, shortcut, action, label, keymap=""):
    from .native_bridge import _action_identity, _stored_action_entry

    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR, "")
    label = str(label).strip()
    if not label:
        return (False, T("Enter a name"), "")
    profile = profiles.setdefault(_profile_key(context), _empty_profile())
    action = _stored_action_entry(action, label=label)
    for key, record in profile["shortcuts"].items():
        if record["shortcut"] == shortcut and _action_identity(record["action"]) == _action_identity(action):
            return (True, T("This shortcut source already exists"), key)
    key = uuid.uuid4().hex
    profile["shortcuts"][key] = {"label": label, "shortcut": copy.deepcopy(shortcut), "action": action, "keymap": str(keymap)}
    ok, message = _save_profiles(profiles)
    return (ok, message, key if ok else "")


def command_entries(context, profile=None):
    profile = load_profile(context) if profile is None else profile
    result = []
    for key, record in profile["commands"].items():
        entry = copy.deepcopy(record["action"])
        entry.update(label=record["label"], command_id=key)
        result.append(entry)
    return result


def action_sources(context, profile=None):
    profile = load_profile(context) if profile is None else profile
    return shortcut_entries(context, profile) + command_entries(context, profile)


def save_command(context, operator_id, label=""):
    from .commands import inspect_command
    from .native_bridge import _stored_action_entry

    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR, "")
    try:
        operator_id, default_label = inspect_command(operator_id)
    except ValueError as exc:
        return (False, str(exc), "")
    label = str(label).strip() or default_label
    profile = profiles.setdefault(_profile_key(context), _empty_profile())
    for key, record in profile["commands"].items():
        if record["action"]["operator"] == operator_id:
            return (True, T("This manual command already exists; its name is kept"), key)
    key = uuid.uuid4().hex
    entry = _stored_action_entry({"kind": "ACTION", "action_type": "OPERATOR", "operator": operator_id,
                                 "label": label, "properties": {}, "opcontext": 0, "manual_command": True})
    profile["commands"][key] = {"label": label, "action": entry}
    ok, message = _save_profiles(profiles)
    return (ok, message, key if ok else "")


def rename_command(context, key, label):
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    profile = profiles.get(_profile_key(context), _empty_profile())
    label = str(label).strip()
    if key not in profile["commands"] or not label:
        return (False, T("The manual command does not exist or the name is empty"))
    profile["commands"][key]["label"] = label
    for layer in [profile, *profile["folders"].values(), *profile["groups"].values()]:
        for entry in layer["slots"]:
            if entry and entry.get("command_id") == key:
                entry["label"] = label
    return _save_profiles(profiles)


def delete_command(context, key):
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    profile = profiles.get(_profile_key(context), _empty_profile())
    if key not in profile["commands"]:
        return (False, T("The manual command no longer exists"))
    del profile["commands"][key]
    return _save_profiles(profiles)


def rename_shortcut(context, key, label):
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    profile = profiles.get(_profile_key(context), _empty_profile())
    label = str(label).strip()
    if key not in profile["shortcuts"] or not label:
        return (False, T("The shortcut source does not exist or the name is empty"))
    profile["shortcuts"][key]["label"] = label
    for layer in [profile, *profile["folders"].values(), *profile["groups"].values()]:
        for entry in layer["slots"]:
            if entry and entry.get("shortcut_id") == key:
                entry["label"] = label
    return _save_profiles(profiles)


def remove_action_references(context, action):
    """Clear every wheel slot that places this favorite in the current profile.

Slots hold action snapshots, so removing a native Quick Favorite used to leave
copies behind on the wheel, in sub-wheels and inside groups.  Clear them in
the same profile the favorites list belongs to.  Entries placed from the action
library (shortcut or command sources) are a separate origin and stay.
Returns (ok, message, cleared_count).
"""
    from .native_bridge import _action_identity

    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR, 0)
    key = _profile_key(context)
    if key not in profiles:
        return (True, "", 0)
    profile = profiles[key]
    identity = _action_identity(action)
    cleared = 0
    for layer in [profile, *profile["folders"].values(), *profile["groups"].values()]:
        slots = layer["slots"]
        for index, entry in enumerate(slots):
            if (
                entry
                and entry.get("kind") == "ACTION"
                and not entry.get("shortcut_id")
                and not entry.get("command_id")
                and _action_identity(entry) == identity
            ):
                slots[index] = None
                cleared += 1
    if not cleared:
        return (True, "", 0)
    ok, message = _save_profiles(profiles)
    return (ok, message, cleared if ok else 0)


def delete_shortcut(context, key):
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    profile = profiles.get(_profile_key(context), _empty_profile())
    if key not in profile["shortcuts"]:
        return (False, T("The shortcut source no longer exists"))
    del profile["shortcuts"][key]
    # Placed entries are detached action snapshots, not live key events.
    return _save_profiles(profiles)


def create_folder(context, label, kind="FOLDER"):
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR, "")
    label = label.strip()
    if not label:
        return (False, T("Enter a sub-wheel name"), "")
    folder_id = uuid.uuid4().hex
    profile = profiles.setdefault(_profile_key(context), _empty_profile())
    count = GROUP_SLOT_COUNT if kind == "GROUP" else SLOT_COUNT
    profile[LIBRARIES[kind]][folder_id] = {"label": label, "slots": _empty_slots(count)}
    ok, message = _save_profiles(profiles)
    return (ok, message, folder_id if ok else "")


def rename_folder(context, folder_id, label, kind="FOLDER"):
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    label = label.strip()
    if not label:
        return (False, T("Enter a sub-wheel name"))
    profile = profiles.get(_profile_key(context), _empty_profile())
    library = profile[LIBRARIES[kind]]
    if folder_id not in library:
        return (False, T("The sub-wheel or group no longer exists"))
    library[folder_id]["label"] = label
    return _save_profiles(profiles)


def delete_folder(context, folder_id, kind="FOLDER"):
    profiles = _load_profiles()
    if _LOAD_ERROR:
        return (False, _LOAD_ERROR)
    profile = profiles.get(_profile_key(context), _empty_profile())
    library = profile[LIBRARIES[kind]]
    if folder_id not in library:
        return (False, T("The sub-wheel or group no longer exists"))
    del library[folder_id]
    for layer in [profile, *profile["folders"].values(), *profile["groups"].values()]:
        layer["slots"] = [None if entry and entry.get("kind") == kind and entry.get("layer_id") == folder_id else entry
                          for entry in layer["slots"]]
    return _save_profiles(profiles)
