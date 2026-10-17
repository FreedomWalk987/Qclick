# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)
"""Shared runtime infrastructure; no layout, action or persistence policy."""
import logging

import bpy

log = logging.getLogger(__package__)

# Popup dialog widths, unscaled; Blender applies the UI scale itself.
DIALOG_WIDTH_SMALL = 300   # a single name field
DIALOG_WIDTH_MEDIUM = 420  # a short form or list
DIALOG_WIDTH_LARGE = 560   # import previews and backup lists

# A GPU draw handler is registered on one space class, so anything drawn over
# an editor must use that editor's class or it never renders there.
SPACE_CLASS_BY_AREA = {
    "VIEW_3D": "SpaceView3D",
    "IMAGE_EDITOR": "SpaceImageEditor",
}


def space_class(area_type, default="SpaceView3D"):
    return getattr(bpy.types, SPACE_CLASS_BY_AREA.get(str(area_type or ""), default))


def live_area(area_pointer):
    """Resolve an area from the live window list, or None if it is gone.

Blender's Area and Region objects are raw pointers with no reference counting.
Once Blender frees one -- an area joined, closed, or switched to another
editor -- any Python object still holding it reads freed memory, which crashes
the process outright rather than raising.  Only objects reached by walking the
current window list are safe to dereference, so anything that outlives a single
event must keep the pointer as an integer and resolve it again here.
"""
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        return None
    wanted = int(area_pointer or 0)
    if not wanted:
        return None
    for window in windows:
        try:
            screen = window.screen
        except (ReferenceError, AttributeError):
            continue
        if screen is None:
            continue
        for area in screen.areas:
            if int(area.as_pointer()) == wanted:
                return area
    return None


def live_override(window_pointer, screen_pointer, area_pointer, region_pointer, area_type):
    """Resolve a complete original editor context using integer identities.

    Never dereference captured RNA objects after an event boundary. Requiring
    the entire live hierarchy prevents executing in an unrelated window when
    just one captured reference survives a screen or editor change.
    """
    if not all((window_pointer, screen_pointer, area_pointer, region_pointer)):
        return {}
    for window in bpy.context.window_manager.windows:
        if int(window.as_pointer()) != window_pointer:
            continue
        screen = window.screen
        if screen is None or int(screen.as_pointer()) != screen_pointer:
            return {}
        for area in screen.areas:
            if int(area.as_pointer()) != area_pointer:
                continue
            if area.type != area_type:
                return {}
            for region in area.regions:
                if region.type == "WINDOW" and int(region.as_pointer()) == region_pointer:
                    return dict(window=window, screen=screen, area=area, region=region)
            return {}
        return {}
    return {}


def redraw_area(area_pointer, kinds=("UI", "WINDOW")):
    """Redraw by pointer, skipping areas that no longer exist."""
    redraw_regions(live_area(area_pointer), kinds)


def redraw_regions(area, kinds=("UI", "WINDOW")):
    """Do not invalidate unrelated headers and toolbars on overlay updates."""
    if area is None:
        return
    try:
        regions = tuple(area.regions)
    except (ReferenceError, AttributeError):
        return
    for region in regions:
        try:
            if region.type in kinds:
                region.tag_redraw()
        except ReferenceError:
            continue


class Registration:
    """Own successful setup steps and unwind them in reverse order."""
    def __init__(self):
        self.cleanups = []
        self.active = False

    def start(self, steps):
        if self.active:
            return
        if self.cleanups:
            raise RuntimeError("Qclick cleanup is incomplete; restart Blender before enabling")
        try:
            for setup, cleanup in steps:
                # Include a partially completed setup in rollback.
                self.cleanups.append(cleanup)
                setup()
            self.active = True
        except Exception:
            self.stop()
            raise

    def stop(self):
        self.active = False
        pending, self.cleanups = self.cleanups, []
        failed = []
        for cleanup in reversed(pending):
            try:
                cleanup()
            except Exception:
                log.exception("Qclick resource cleanup failed: %r", cleanup)
                failed.append(cleanup)
        # Retain failures for a later explicit cleanup attempt, not silent loss.
        self.cleanups = list(reversed(failed))
