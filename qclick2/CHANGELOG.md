# Changelog

All notable changes to Q&Click. Versions follow `MAJOR.MINOR.PATCH`:
PATCH fixes bugs, MINOR adds features, MAJOR breaks compatibility.

## 1.0.0 — first public release

- Hold-Q quick-favorites wheel for the 3D Viewport and UV/Image Editor, one
  layout per mode, with sub-wheels, four-item groups and a repeat-last action.
- Sidebar drag editor (N panel, "Q&Click" tab): drag Blender Quick Favorites
  or action-library items onto eight slots; reorder and remove native Quick
  Favorites; rename, clear and reset (reset asks for typed confirmation).
- Settings export/import (merge or replace) with automatic backups and
  restore-from-backup.
- English and Traditional Chinese interface, following Blender's language;
  an "Interface Language" add-on preference can force either.
- Tested on Blender 5.0.1 (native Quick Favorites read, reorder, remove,
  save and reload; install, enable, disable and re-enable of both packages).

---

## Pre-release history (private builds 1.26.x, formerly "Q&Click 2.0")

### 1.26.14


- The Reset and Clear Slot buttons sit 2 px lower in the wheel panel.

## Changes in 1.26.13

- Fix: icons no longer show a light square. Drawing text changed the GPU
  blend state, so later translucent pixels were written without blending.
  Alpha blending is now restored after every text draw, and icon pixels are
  precomposed onto the button colour so they are fully opaque.

## Changes in 1.26.12

- Fix: the icons showed a solid light square behind them because the texture
  path dropped transparency. Each icon pixel is now drawn with its own
  coverage as alpha, through the same shader as the panel gradients.

## Changes in 1.26.11

- Icons are now drawn the way Blender draws its own: pre-rendered,
  anti-aliased 16 px bitmaps placed on whole pixels, with thin strokes. The
  Reset icon takes the shape of Blender's loop-back arrow. This replaces the
  live-line icons of 1.26.10, which looked thick and blurry in Blender.

## Changes in 1.26.10

- The Reset and Clear Slot icons are redrawn with Blender's anti-aliased
  wide-line shader, so they have smooth edges and an even stroke instead of
  jagged pixels. The Reset icon is larger and reshaped as the common
  anticlockwise arrow with a square arrow corner.

## Changes in 1.26.9

- Reset now opens a confirmation dialog naming the layer it will clear. Type
  `reset` (any letter case) and press Reset to clear it; anything else leaves
  the wheel unchanged. The old press-twice confirmation is gone.

## Changes in 1.26.8

- The Reset icon button now sits directly left of Clear Slot instead of at the
  far left of the wheel footer.

## Changes in 1.26.7

- Sidebar wheel footer: Reset is now a small square button with a reset
  (anticlockwise arrow) icon at the left; hovering it explains what it does.
  Clear Slot moves to the right half, where Reset used to be, and gains an X
  icon. Reset still needs a second click to confirm.
- The status hint after creating a sub-wheel or group pointed at a centre
  "Edit" button that no longer exists; it now points at the pencil button.
- Wording is unified: Blender's Quick Favorites are always "快速收藏", and the
  repeat feature is always "上一步".
- Sidebar text padding, the scrollbar inset and minimum thumb, and the drag
  card margin now follow the UI scale like everything else.
- Popup dialogs use three widths (300, 420, 560) instead of six.
- Unused code is removed: the hidden centre "Edit" button, the 1.26.4 edge-fit
  and old two-column wheel layouts, a deprecated group helper, unused
  constants, and font-size fallbacks for Blender versions before 4.0. The wheel
  behaves exactly as in 1.26.6.

## Changes in 1.26.6

- The wheel is drawn exactly where Q is pressed again, as in 1.26.3. The
  1.26.4 change that slid the overlay inside the viewport near its edges is
  removed; cards near an edge may be drawn partly off screen, as before.

## Changes in 1.26.5

- Export settings: the "Settings Import and Export" panel saves every mode's
  wheels, sub-wheels, groups and action library to a JSON file of your choice.
- Import Q&Click 2.0 settings: merge (replace only the modes in the file) or
  replace everything. The current settings are backed up first as
  `slots.pre-import-<date>.json.bak` and can be brought back with "restore
  from backup". The newest ten import backups are kept.
- Quick Favorites themselves are not part of the export; they belong to
  Blender's preferences. Wheel slots carry their own copy of each action, so
  imported wheels work without them.

## Changes in 1.26.4

- Reordering or removing a native Quick Favorite no longer saves the user's
  other unsaved preference edits. When other edits are pending, the change
  stays in memory with them (auto-save on quit, or Save Preferences) and the
  status line says so. The one-time backup copies the preferences file as last
  saved instead of saving first.
- Preference backups in `qclick2_backups` are pruned to the newest 10. Only
  files this add-on created are ever removed.
- When `slots.json` cannot be read, the Settings Import panel shows the error
  and a "restore from backup" button. Only backups that pass full validation
  are offered; the current file is kept as `slots.broken-<date>.json`.

## Changes in 1.26.3

- Blender's autosave now runs while Q&Click 2 is enabled. Blender skips
  autosave whenever a modal operator is running, and the sidebar editor used
  to keep one running permanently. The editor now receives input only while
  the pointer is over the Q&Click 2 sidebar; its tab, layer, scroll and
  selection are kept when the pointer leaves.
- Sidebars showing another tab are no longer redrawn five times a second.
- Toggling a data property from the wheel (for example an object or modifier
  setting) creates its own undo step, so one Ctrl+Z reverts only the toggle.
  View settings such as overlays stay out of the undo history, as in Blender.
- If the release that confirms a wheel choice never arrives, input is handed
  back after four seconds instead of being held, and the action is not run.
- Shortcut capture in the UV/Image Editor reads that editor's keymaps instead
  of the 3D View ones.

## Changes in 1.26.2

- Removing a native Quick Favorite now also clears its copies from the wheel,
  sub-wheels and groups of the same mode. Copies are kept when the action is
  still a favorite through another native menu, and entries placed from the
  action library are not touched.

## Changes in 1.26.1

- Verify the native UserDef layout against Blender's own RNA (ui_scale, the
  themes and addons list heads) before reading or writing Quick Favorites.
  A layout that does not match the running build is refused before any
  native list is walked.
- Report a busy settings lock from another Blender instance as a clear message.

## Changes in 1.26.0

- Remove references from all parent groups when deleting a nested group.
- Resolve deferred actions against the original live window, screen, editor,
  and region without retaining freed RNA references.
- Cancel old overlays and sidebar sessions before loading another blend file.
- Reject malformed stored actions while preserving unavailable third-party tools.
- Stage settings in unique files, flush them, detect changed settings, and
  coordinate concurrent Blender writers using operating-system file locks.
- Add the Blender 5.2 UserDef layout and correct operator property decoding.

Settings remain in Blender's configuration directory under `qclick2/slots.json`.
The schema remains version 9. Valid existing layouts retain their meaning.
`slots.json.bak` stores the prior settings before replacement. A small
`slots.json.lock` file coordinates writers; its mere presence does not mean a
lock is held. Operating-system locks are released when a process exits.

## Rollback

Disable this version, reinstall the preserved baseline ZIP, and restart Blender.
If restoring settings is necessary, close Blender first, retain a copy of the
current JSON, and restore the relevant backup to `slots.json`. Do not overwrite
your current settings without keeping a copy.
