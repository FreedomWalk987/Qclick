# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 FreedomWalk (Harry_Yeh)

"""Pure four-action branch layout and hit testing, independent of Blender.

The compact cardinal fan and diagonal rail follow the audited 1.x model.
Layout is resolved once on entry and shared by drawing and hit testing.
"""

import math

DIRECTIONS = ((-1, 0), (1, 0), (0, -1), (0, 1),
              (-math.sqrt(0.5), math.sqrt(0.5)), (math.sqrt(0.5), math.sqrt(0.5)),
              (-math.sqrt(0.5), -math.sqrt(0.5)), (math.sqrt(0.5), -math.sqrt(0.5)))


# Fixed slot geometry: item N always lands on the same spot, whether or not
# its neighbours exist, so a group can be arranged by position, not fill order.
# Left/right groups sit a little further out than they used to, so their
# items clear the centre marker by roughly the same margin as an up/down group.
FIXED_TEMPLATE = ((48, -76), (92, -26), (92, 26), (48, 76))

# Up/down groups keep their own tuned geometry, unchanged by the wider
# left/right spacing above: flanks pushed out to the sides and lifted toward
# the centre row, inner pair straight out.
VERTICAL_ANCHORS = (2, 3)
VERTICAL_FLANK_SLOTS = (0, 3)
VERTICAL_TEMPLATE = ((26, -88), (72, -26), (72, 26), (26, 88))


def slot_template(index, anchor_slot=0):
    """Outward distance and tangent offset for a stored group slot index."""
    index = int(index) % 4
    if anchor_slot in VERTICAL_ANCHORS:
        return VERTICAL_TEMPLATE[index]
    return FIXED_TEMPLATE[index]


def rect_at(position, size):
    x, y = position
    width, height = size
    return (x - width / 2, y - height / 2, x + width / 2, y + height / 2)


def contains(point, rect, padding=0):
    return rect[0] - padding <= point[0] <= rect[2] + padding and rect[1] - padding <= point[1] <= rect[3] + padding


def overlap(first, second, gap=0):
    return max(0, min(first[2], second[2]) - max(first[0], second[0]) + gap) * max(
        0, min(first[3], second[3]) - max(first[1], second[1]) + gap)


# --- diagonal anchors only (left-up / right-up / left-down / right-down) ---
# Matched to the up/down group spacing rather than the old tight box.
KEEPOUT_X = 50.0
KEEPOUT_Y = 56.0
FAR_SIDE_DROP = 0.6

DIAGONAL_SIDE = {4: -1, 5: 1, 6: -1, 7: 1}


def _diagonal_positions(sizes, center, anchor_slot):
    """Vertical stack on the anchor's side, edge-aligned to the keep-out box."""
    side = DIAGONAL_SIDE[anchor_slot]
    places = ("U", "S", "D", "F")  # slot 1..4, fixed regardless of how many exist
    positions = {}
    for index in sizes:
        place = places[int(index) % 4]
        width, height = sizes[index]
        if place == "U":
            positions[index] = (center[0] + side * width / 2.0,
                                center[1] + KEEPOUT_Y + height / 2.0)
        elif place == "D":
            positions[index] = (center[0] + side * width / 2.0,
                                center[1] - KEEPOUT_Y - height / 2.0)
        elif place == "S":
            positions[index] = (center[0] + side * (KEEPOUT_X + width / 2.0),
                                center[1])
        else:
            positions[index] = (center[0] - side * (KEEPOUT_X + width / 2.0),
                                center[1] - height * FAR_SIDE_DROP)
    return positions


def resolve_positions(sizes, center, anchor_slot, region_size):
    """Prefer the anchor's outward fan; turn inward near viewport edges.

Pass region_size=None for position-invariant layout without edge avoidance.

Only occupied items participate, preserving their stored indices and order.
Increase tangent spacing for long labels before accepting overlap. Extremely
small viewports return the least-colliding candidate rather than moving a cursor.
"""
    if not sizes:
        return {}
    if anchor_slot in DIAGONAL_SIDE:
        return _diagonal_positions(sizes, center, anchor_slot)
    indices = tuple(sizes)
    preferred = DIRECTIONS[anchor_slot]
    directions = sorted(range(8), key=lambda index: -(DIRECTIONS[index][0] * preferred[0] + DIRECTIONS[index][1] * preferred[1]))
    width, height = region_size or (0, 0)
    center_rect = rect_at(center, (68, 68))
    best, best_score = {}, (float("inf"),) * 3
    for direction in directions:
        outward = DIRECTIONS[direction]
        # Reading order must match the editor list: slot 1 is always the top
        # item (or the leftmost one when the group opens up or down), never
        # mirrored by the rotation direction.
        tangent = (0.0, -1.0) if abs(outward[0]) >= abs(outward[1]) else (1.0, 0.0)
        for spread_step in range(25):
            spread = 1 + spread_step * 0.125
            for outward_step in range(15):
                positions = {}
                for index in indices:
                    distance, offset = slot_template(index, anchor_slot)
                    distance += outward_step * 8
                    # The up/down flanking pair keeps its fixed side offset;
                    # only the inner pair spreads to clear long labels.
                    if not (anchor_slot in VERTICAL_ANCHORS
                            and int(index) % 4 in VERTICAL_FLANK_SLOTS):
                        offset *= spread
                    positions[index] = (center[0] + outward[0] * distance + tangent[0] * offset,
                                        center[1] + outward[1] * distance + tangent[1] * offset)
                rects = [rect_at(positions[index], sizes[index]) for index in indices]
                collisions = [overlap(rect, center_rect) for rect in rects]
                collisions += [overlap(first, second, 12) for i, first in enumerate(rects) for second in rects[i + 1:]]
                overflow = sum(max(0, 8 - r[0]) + max(0, 8 - r[1]) + max(0, r[2] - width + 8) + max(0, r[3] - height + 8) for r in rects)
                if region_size is None:
                    overflow = 0
                score = (sum(area > 0 for area in collisions), sum(collisions), overflow)
                if score < best_score:
                    best, best_score = positions, score
                if score == (0, 0, 0):
                    return positions
    return best


def hit(mouse, center, positions, sizes, deadzone=34):
    dx, dy = mouse[0] - center[0], mouse[1] - center[1]
    distance = math.hypot(dx, dy)
    if distance <= deadzone:
        return -1
    for index, position in positions.items():
        if contains(mouse, rect_at(position, sizes[index]), 4):
            return index
    best_index, best_dot = -1, 0.25
    for index, position in positions.items():
        px, py = position[0] - center[0], position[1] - center[1]
        length = math.hypot(px, py)
        dot = (dx * px + dy * py) / max(1e-6, distance * length)
        if dot > best_dot:
            best_index, best_dot = index, dot
    return best_index
