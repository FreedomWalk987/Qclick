# Q&Click

Hold **Q**, flick toward an action, release. Q&Click turns Blender's Quick Favorites into a
radial wheel — one layout per mode — and gives you a drag-and-drop sidebar editor to arrange it.

- Version: 1.0.0
- Blender 4.5 – 5.2 (tested on 5.0.1), **Windows 64-bit only**
- Interface: English and Traditional Chinese (follows Blender's language)
- License: GPL-3.0-or-later, free and open source

[繁體中文說明在下方](#繁體中文)

## Features

- **Wheel**: hold Q in the 3D Viewport or UV/Image Editor. Eight directions, a separate layout for
  every mode (Object, Edit Mesh, Sculpt…).
- **Sub-wheels and groups**: put a whole set behind one slot — a sub-wheel opens a full new wheel,
  a group fans out four items next to the slot.
- **Repeat last**: double-tap Q (or Q + left click) to run the last wheel action again.
- **Sidebar editor**: the "Q&Click" tab in the N panel. Drag from Favorites or the action library
  onto the eight slots; right-click to rename.
- **Manage Blender's Quick Favorites**: reorder and remove native Quick Favorites from the sidebar.
- **Action library**: capture a shortcut or type an operator ID, without adding it to Quick Favorites first.
- **Settings export/import**: wheels, groups, action library **and Blender's Quick Favorites** in one
  file; move to another computer or share with colleagues. A backup is made before every import.

## Install

Install either file; they work the same:

| File | Use it if |
|---|---|
| `QClick-1.1.1-legacy.zip` | you install add-ons the traditional way |
| `QClick-1.1.1-extension.zip` | you install Blender Extensions |

1. Blender → Edit → Preferences → Add-ons (or Extensions).
2. Top-right menu → **Install from Disk**, pick the zip.
3. Enable **Q&Click**.

When updating, disable the old version, install the new one, then restart Blender.

## Use

1. In the 3D Viewport press **N** and open the **Q&Click** tab.
2. Drag actions from the lower list onto the eight slots above.
3. Back in the viewport, **hold Q**, move toward the action, **release**.

**Clear Slot** (✕) clears the selected slot. Reset (↩) clears the whole wheel and asks you to type
`reset` to confirm.

**Language**: Q&Click follows Blender's interface language (Preferences → Interface → Translation).
To force English or Chinese, change **Interface Language** in Q&Click's add-on preferences.

## Good to know

- Blender has no Python API for editing Quick Favorites, so Q&Click **reads and writes Blender's
  internal data** to reorder, remove and import native favorites. Every edit first checks that the data
  layout matches the running Blender, and refuses if it does not.
- Before the first change to native Quick Favorites, your preferences are backed up to
  `%APPDATA%\Blender Foundation\Blender\<version>\config\qclick2_backups\` (newest 10 kept).
- Wheel layouts live in `qclick2\slots.json` in the same config folder. The **Settings Import and
  Export** panel can export, import, or restore from a backup.
- Windows only. Each future Blender series needs to be verified before it is supported; on an
  unsupported version Quick Favorites editing is disabled, but the wheel still works.

## Reporting problems

Open a GitHub issue with your Blender version, the steps, and any error message
(Window → Toggle System Console).

## License

GPL-3.0-or-later. Copyright (C) 2026 FreedomWalk (Harry_Yeh). See `LICENSE.txt`.

---

## 繁體中文

按住 **Q**，滑向方向，放開就執行。Q&Click 把 Blender 的「快速收藏」變成依模式切換的輪盤，
並在側欄提供拖曳編輯器。

- 支援 Blender 4.5 – 5.2（於 5.0.1 實測），**僅限 Windows 64 位元**
- 介面：英文與繁體中文，跟隨 Blender 的介面語言
- 授權：GPL-3.0-or-later，免費開源

**安裝**：偏好設定 → 附加元件（或擴充功能）→ 右上角選單 → 從磁碟安裝，選擇 zip 後勾選啟用。
兩個 zip 擇一即可。

**使用**：3D 視窗按 **N** 開啟側欄的 **Q&Click** 分頁，把功能拖到上方 8 個槽位；
回到視窗**按住 Q**、滑向目標、**放開**執行。快速按兩下 Q（或 Q＋左鍵）重複上一步。
在收藏或槽位上按右鍵可改顯示名稱；重設（↩）需輸入 `reset` 確認。

**語言**：預設跟隨 Blender 的介面語言（偏好設定 → 介面 → 翻譯）。若 Blender 是英文介面但想看中文，
在 Q&Click 的附加元件偏好設定把「介面語言」改成「繁體中文」。

**請先知道**：Q&Click 會直接讀寫 Blender 內部資料來排序、移除原生快速收藏，每次操作前都會核對資料結構，
不符就拒絕執行。第一次修改前會把偏好設定備份到 config 資料夾的 `qclick2_backups`；輪盤設定存在
`qclick2\slots.json`，可在「設定匯入與匯出」面板匯出、匯入或從備份還原。
