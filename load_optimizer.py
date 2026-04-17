#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import copy
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# API-style payloads for future integration.
# Replace these dicts with API response JSON when needed.
GOODS_PAYLOAD: Dict[str, Any] = {
    "goods": [
        # {
        #     "id": "10009399",
        #     "name": "spooler unit",
        #     "weight_kg": 4500,
        #     "l": 3.14,
        #     "b": 2.68,
        #     "h": 2.95,
        #     "stackable": True,
        #     "max_stack": 1,
        #     "adr": False,
        #     "adr_class": "",
        # }
    ]
}

TRUCKS_PAYLOAD: Dict[str, Any] = {
    "trucks": [
        # {
        #     "id": "1",
        #     "name": "JUMBO Extendable trailer",
        #     "l": 14.5,
        #     "b": 4.0,
        #     "h": 2.95,
        #     "max_weight_kg": 100000,
        # }
    ]
}


@dataclass
class Item:
    item_id: str
    name: str
    weight: float
    l: float
    w: float
    h: float
    stackable: bool
    max_stack: int
    adr: bool = False
    adr_class: str = ""

    def rotations_xy(self) -> List[Tuple[float, float, float]]:
        # Keep vertical axis as height; only rotate on floor plane.
        if abs(self.l - self.w) < 1e-9:
            return [(self.l, self.w, self.h)]
        return [(self.l, self.w, self.h), (self.w, self.l, self.h)]

    @property
    def footprint(self) -> float:
        return self.l * self.w

    @property
    def max_side(self) -> float:
        return max(self.l, self.w)

    @property
    def min_side(self) -> float:
        return min(self.l, self.w)

    @property
    def is_long_flat(self) -> bool:
        return self.max_side >= 1.8 and self.h <= 0.35


@dataclass
class TruckType:
    truck_id: str
    name: str
    l: float
    w: float
    h: float
    max_weight: float


@dataclass
class Placement:
    item: Item
    x: float
    y: float
    z: float
    l: float
    w: float
    h: float
    truck_idx: int
    base_item_indices: List[int] = field(default_factory=list)
    stack_level: int = 0
    top_free_rects: List[Tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def x2(self) -> float:
        return self.x + self.l

    @property
    def y2(self) -> float:
        return self.y + self.w

    @property
    def z2(self) -> float:
        return self.z + self.h


@dataclass
class TruckLoad:
    truck_type: TruckType
    placements: List[Placement] = field(default_factory=list)
    current_weight: float = 0.0
    used_length: float = 0.0
    free_rects: List[Tuple[float, float, float, float]] = field(default_factory=list)

    def can_take_weight(self, weight: float) -> bool:
        return self.current_weight + weight <= self.truck_type.max_weight

    def __post_init__(self) -> None:
        if not self.free_rects:
            self.free_rects = [(0.0, 0.0, self.truck_type.l, self.truck_type.w)]


def parse_items_from_json(payload: Dict[str, Any]) -> List[Item]:
    items: List[Item] = []
    for row in payload.get("goods", []):
        items.append(
            Item(
                item_id=str(row["id"]),
                name=str(row.get("name", "")),
                weight=float(row["weight_kg"]),
                l=float(row["l"]),
                w=float(row["b"]),
                h=float(row["h"]),
                stackable=bool(row.get("stackable", True)),
                max_stack=int(row.get("max_stack", 1)),
                adr=bool(row.get("adr", False)),
                adr_class=str(row.get("adr_class", "") or "").strip(),
            )
        )
    return items


def parse_trucks_from_json(payload: Dict[str, Any]) -> List[TruckType]:
    trucks: List[TruckType] = []
    for row in payload.get("trucks", []):
        trucks.append(
            TruckType(
                truck_id=str(row["id"]),
                name=str(row.get("name", "")),
                l=float(row["l"]),
                w=float(row["b"]),
                h=float(row["h"]),
                max_weight=float(row["max_weight_kg"]),
            )
        )
    trucks.sort(key=lambda t: (t.l, t.w * t.h, t.max_weight), reverse=True)
    return trucks


def read_goods(path: str) -> List[Item]:
    items: List[Item] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            items.append(
                Item(
                    item_id=row["id"],
                    name=row.get("name", ""),
                    weight=float(row["weight_kg"]),
                    l=float(row["l"]),
                    w=float(row["b"]),
                    h=float(row["h"]),
                    stackable=row.get("stackable", "false").strip().lower() == "true",
                    max_stack=int(row.get("max_stack", "1") or 1),
                    adr=row.get("adr", "false").strip().lower() == "true",
                    adr_class=(row.get("adr_class", "") or "").strip(),
                )
            )
    return items


def read_trucks(path: str) -> List[TruckType]:
    trucks: List[TruckType] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trucks.append(
                TruckType(
                    truck_id=row["id"],
                    name=row.get("name", ""),
                    l=float(row["l"]),
                    w=float(row["b"]),
                    h=float(row["h"]),
                    max_weight=float(row["max_weight_kg"]),
                )
            )
    # Prefer longer trucks first; this reduces split loads for long freight.
    trucks.sort(key=lambda t: (t.l, t.w * t.h, t.max_weight), reverse=True)
    return trucks


def overlap_2d(a: Placement, x: float, y: float, l: float, w: float) -> bool:
    return not (x + l <= a.x or a.x2 <= x or y + w <= a.y or a.y2 <= y)


def rect_overlap(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
    eps: float = 1e-9,
) -> Optional[Tuple[float, float, float, float]]:
    ax, ay, al, aw = a
    bx, by, bl, bw = b
    ox1 = max(ax, bx)
    oy1 = max(ay, by)
    ox2 = min(ax + al, bx + bl)
    oy2 = min(ay + aw, by + bw)
    if (ox2 - ox1) <= eps or (oy2 - oy1) <= eps:
        return None
    return (ox1, oy1, ox2 - ox1, oy2 - oy1)


def subtract_rect(
    rect: Tuple[float, float, float, float],
    cut: Tuple[float, float, float, float],
    eps: float = 1e-9,
) -> List[Tuple[float, float, float, float]]:
    overlap = rect_overlap(rect, cut, eps)
    if overlap is None:
        return [rect]
    rx, ry, rl, rw = rect
    ox, oy, ol, ow = overlap
    rx2, ry2 = rx + rl, ry + rw
    ox2, oy2 = ox + ol, oy + ow
    out: List[Tuple[float, float, float, float]] = []
    if (ox - rx) > eps:
        out.append((rx, ry, ox - rx, rw))
    if (rx2 - ox2) > eps:
        out.append((ox2, ry, rx2 - ox2, rw))
    if (oy - ry) > eps:
        out.append((ox, ry, ol, oy - ry))
    if (ry2 - oy2) > eps:
        out.append((ox, oy2, ol, ry2 - oy2))
    return out


def merge_free_rectangles(
    rects: List[Tuple[float, float, float, float]],
    eps: float = 1e-9,
    min_dim: float = 0.01,
) -> List[Tuple[float, float, float, float]]:
    out = [r for r in rects if r[2] > min_dim and r[3] > min_dim]
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            if changed:
                break
            x1, y1, l1, w1 = out[i]
            for j in range(i + 1, len(out)):
                x2, y2, l2, w2 = out[j]
                if abs(y1 - y2) <= eps and abs(w1 - w2) <= eps:
                    if abs((x1 + l1) - x2) <= eps:
                        out[i] = (x1, y1, l1 + l2, w1)
                        out.pop(j)
                        changed = True
                        break
                    if abs((x2 + l2) - x1) <= eps:
                        out[i] = (x2, y1, l1 + l2, w1)
                        out.pop(j)
                        changed = True
                        break
                if abs(x1 - x2) <= eps and abs(l1 - l2) <= eps:
                    if abs((y1 + w1) - y2) <= eps:
                        out[i] = (x1, y1, l1, w1 + w2)
                        out.pop(j)
                        changed = True
                        break
                    if abs((y2 + w2) - y1) <= eps:
                        out[i] = (x1, y2, l1, w1 + w2)
                        out.pop(j)
                        changed = True
                        break
    return out


def guillotine_side_front(
    rx: float, ry: float, rl: float, rw: float, il: float, iw: float
) -> Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]]:
    if (rl - il) > (rw - iw):
        side = (rx, ry + iw, il, rw - iw)
        front = (rx + il, ry, rl - il, rw)
    else:
        side = (rx, ry + iw, rl, rw - iw)
        front = (rx + il, ry, rl - il, iw)
    return side, front


def overlap_3d(a: Placement, x: float, y: float, z: float, l: float, w: float, h: float) -> bool:
    return not (
        x + l <= a.x
        or a.x2 <= x
        or y + w <= a.y
        or a.y2 <= y
        or z + h <= a.z
        or a.z2 <= z
    )


def within_truck(truck: TruckLoad, x: float, y: float, z: float, l: float, w: float, h: float) -> bool:
    t = truck.truck_type
    return x >= 0 and y >= 0 and z >= 0 and x + l <= t.l and y + w <= t.w and z + h <= t.h


def support_ratio(
    truck: TruckLoad,
    x: float,
    y: float,
    z: float,
    l: float,
    w: float,
) -> Tuple[float, List[int]]:
    # Returns covered area ratio and supporting base indices with exact same top z.
    if abs(z) < 1e-9:
        return 1.0, []
    required_top = z
    rectangles: List[Tuple[float, float, float, float, int]] = []
    for i, p in enumerate(truck.placements):
        if abs(p.z2 - required_top) > 1e-7:
            continue
        ix1, iy1 = max(x, p.x), max(y, p.y)
        ix2, iy2 = min(x + l, p.x2), min(y + w, p.y2)
        if ix2 > ix1 and iy2 > iy1:
            rectangles.append((ix1, iy1, ix2, iy2, i))
    if not rectangles:
        return 0.0, []
    overlap_area = union_area_2d([(rx1, ry1, rx2, ry2) for rx1, ry1, rx2, ry2, _ in rectangles])
    base_indices = sorted({idx for _, _, _, _, idx in rectangles})
    area = l * w
    return (overlap_area / area if area > 0 else 0.0), base_indices


def union_area_2d(rectangles: List[Tuple[float, float, float, float]]) -> float:
    if not rectangles:
        return 0.0
    xs = sorted({x1 for x1, _, x2, _ in rectangles} | {x2 for _, _, x2, _ in rectangles})
    total = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        intervals: List[Tuple[float, float]] = []
        for x1, y1, x2, y2 in rectangles:
            if x1 < right and x2 > left:
                intervals.append((y1, y2))
        if not intervals:
            continue
        intervals.sort()
        covered = 0.0
        cur_start, cur_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= cur_end:
                cur_end = max(cur_end, end)
            else:
                covered += cur_end - cur_start
                cur_start, cur_end = start, end
        covered += cur_end - cur_start
        total += (right - left) * covered
    return total


def contiguous_support_ratio(
    truck: TruckLoad,
    x: float,
    y: float,
    z: float,
    l: float,
    w: float,
) -> float:
    # Human loaders typically want the footprint connected, not fragmented.
    if abs(z) < 1e-9:
        return 1.0
    rects: List[Tuple[float, float, float, float]] = []
    for p in truck.placements:
        if abs(p.z2 - z) > 1e-7:
            continue
        ix1, iy1 = max(x, p.x), max(y, p.y)
        ix2, iy2 = min(x + l, p.x2), min(y + w, p.y2)
        if ix2 > ix1 and iy2 > iy1:
            rects.append((ix1, iy1, ix2, iy2))
    if not rects:
        return 0.0
    target = (x + l / 2.0, y + w / 2.0)
    containing = [r for r in rects if r[0] <= target[0] <= r[2] and r[1] <= target[1] <= r[3]]
    if not containing:
        return 0.0
    component = [containing[0]]
    changed = True
    while changed:
        changed = False
        for r in rects:
            if r in component:
                continue
            if any(rectangles_touch_or_overlap(r, c) for c in component):
                component.append(r)
                changed = True
    return union_area_2d(component) / (l * w)


def rectangles_touch_or_overlap(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def base_supports_stack(truck: TruckLoad, base_indices: List[int]) -> bool:
    if not base_indices:
        return True
    for idx in base_indices:
        base = truck.placements[idx]
        if not base.item.stackable:
            return False
        if base.stack_level + 1 >= base.item.max_stack:
            return False
    return True


def can_place(
    truck: TruckLoad,
    x: float,
    y: float,
    z: float,
    l: float,
    w: float,
    h: float,
    min_support_ratio: float = 0.98,
) -> Tuple[bool, List[int]]:
    if not within_truck(truck, x, y, z, l, w, h):
        return False, []
    for p in truck.placements:
        if overlap_3d(p, x, y, z, l, w, h):
            return False, []
    ratio, base_indices = support_ratio(truck, x, y, z, l, w)
    if ratio < min_support_ratio:
        return False, []
    if z > 0:
        if contiguous_support_ratio(truck, x, y, z, l, w) < 0.75:
            return False, []
        if not base_supports_stack(truck, base_indices):
            return False, []
    return True, base_indices


def candidate_positions_floor(truck: TruckLoad) -> List[Tuple[float, float, float]]:
    out: List[Tuple[float, float, float]] = []
    for x, y, _, _ in truck.free_rects:
        out.append((x, y, 0.0))
    return out


def candidate_positions_stacked(truck: TruckLoad) -> List[Tuple[float, float, float]]:
    z_levels = sorted({round(p.z2, 6) for p in truck.placements if p.z2 < truck.truck_type.h})
    if not z_levels:
        return []
    x_candidates = {0.0}
    for p in truck.placements:
        x_candidates.add(round(p.x, 4))
        x_candidates.add(round(p.x2, 4))
    y_candidates = {0.0}
    for p in truck.placements:
        y_candidates.add(round(p.y, 4))
        y_candidates.add(round(p.y2, 4))
    out: List[Tuple[float, float, float]] = []
    for z in z_levels:
        for p in truck.placements:
            if abs(p.z2 - z) < 1e-7:
                x_candidates.add(round(p.x, 4))
                y_candidates.add(round(p.y, 4))
        for x in sorted(x_candidates):
            for y in sorted(y_candidates):
                out.append((x, y, z))
    return out


def floor_contact_score(x: float, y: float, l: float, w: float, truck: TruckLoad) -> Tuple[int, int, int]:
    # Prefer wall-aligned placements and filling toward the back-left corner.
    wall_contacts = 0
    if abs(x) < 1e-7:
        wall_contacts += 1
    if abs(y) < 1e-7:
        wall_contacts += 1
    if abs(y + w - truck.truck_type.w) < 1e-7:
        wall_contacts += 1
    return (-wall_contacts, round(x * 1000), round(y * 1000))


def item_fits_rect(item: Item, rect: Tuple[float, float, float, float]) -> bool:
    _, _, rl, rw = rect
    for il, iw, _ in item.rotations_xy():
        if il <= rl + 1e-9 and iw <= rw + 1e-9:
            return True
    return False


def long_flat_items_remaining(items: List[Item], placed_ids: set[str]) -> List[Item]:
    return [item for item in items if item.item_id not in placed_ids and item.is_long_flat]


def largest_fit_area_for_items(
    items: List[Item],
    rects: List[Tuple[float, float, float, float]],
) -> float:
    best = 0.0
    for item in items:
        for _, _, rl, rw in rects:
            for il, iw, _ in item.rotations_xy():
                if il <= rl + 1e-9 and iw <= rw + 1e-9:
                    best = max(best, il * iw)
    return best


def reserve_fragmentation_penalty(
    item: Item,
    truck: TruckLoad,
    candidate_rect: Tuple[float, float, float, float],
    remaining_long_flat: List[Item],
    rotations: Optional[List[Tuple[float, float, float]]] = None,
) -> float:
    if not remaining_long_flat:
        return 0.0
    ridx_rect = candidate_rect
    rx, ry, rl, rw = ridx_rect
    penalties = 0.0
    for il, iw, _ in (rotations if rotations is not None else item.rotations_xy()):
        if il > rl + 1e-9 or iw > rw + 1e-9:
            continue
        side, front = guillotine_side_front(rx, ry, rl, rw, il, iw)
        future_rects = truck.free_rects.copy()
        future_rects.remove(candidate_rect)
        for rect in (side, front):
            if rect[2] > 0.01 and rect[3] > 0.01:
                future_rects.append(rect)
        future_rects = merge_free_rectangles(future_rects)
        before_fit = largest_fit_area_for_items(remaining_long_flat, truck.free_rects)
        after_fit = largest_fit_area_for_items(remaining_long_flat, future_rects)
        penalties = max(penalties, before_fit - after_fit)
    return penalties


def try_place_on_floor(
    item: Item,
    truck: TruckLoad,
    remaining_items: Optional[List[Item]] = None,
    rotations: Optional[List[Tuple[float, float, float]]] = None,
    max_front_x: Optional[float] = None,
) -> Optional[Placement]:
    best: Optional[Tuple[Tuple[float, float, float, float, float], int, Placement]] = None
    placed_ids = {p.item.item_id for p in truck.placements}
    remaining_long_flat = long_flat_items_remaining(remaining_items or [], placed_ids)
    for ridx, (rx, ry, rl, rw) in enumerate(truck.free_rects):
        for il, iw, ih in (rotations if rotations is not None else item.rotations_xy()):
            if ih > truck.truck_type.h or il > rl + 1e-9 or iw > rw + 1e-9:
                continue
            if max_front_x is not None and rx + il > max_front_x + 1e-9:
                continue
            placement = Placement(
                item=item,
                x=rx,
                y=ry,
                z=0.0,
                l=il,
                w=iw,
                h=ih,
                truck_idx=-1,
                base_item_indices=[],
                stack_level=0,
                top_free_rects=[(0.0, 0.0, il, iw)],
            )
            fragmentation_penalty = reserve_fragmentation_penalty(
                item,
                truck,
                (rx, ry, rl, rw),
                remaining_long_flat,
                rotations=rotations,
            )
            score = (
                round(fragmentation_penalty, 6),
                round(rx, 4),
                round(ry, 4),
                round(rx + il, 4),
                (rl * rw) - (il * iw),
                -iw,
            )
            if best is None or score < best[0]:
                best = (score, ridx, placement)
    if best is None:
        return None
    _, ridx, placement = best
    rx, ry, rl, rw = truck.free_rects[ridx]
    del truck.free_rects[ridx]
    side, front = guillotine_side_front(rx, ry, rl, rw, placement.l, placement.w)
    for rect in (side, front):
        if rect[2] > 0.01 and rect[3] > 0.01:
            truck.free_rects.append(rect)
    truck.free_rects = merge_free_rectangles(truck.free_rects)
    return placement


def rebuild_top_free_rects(truck: TruckLoad, eps: float = 1e-4) -> None:
    for base in truck.placements:
        rects: List[Tuple[float, float, float, float]] = [(0.0, 0.0, base.l, base.w)]
        for q in truck.placements:
            if q is base:
                continue
            if abs(q.z - base.z2) > eps:
                continue
            overlap = rect_overlap((base.x, base.y, base.l, base.w), (q.x, q.y, q.l, q.w), eps)
            if overlap is None:
                continue
            local = (overlap[0] - base.x, overlap[1] - base.y, overlap[2], overlap[3])
            next_rects: List[Tuple[float, float, float, float]] = []
            for rect in rects:
                next_rects.extend(subtract_rect(rect, local, eps))
            rects = merge_free_rectangles(next_rects)
        base.top_free_rects = rects


def try_stack_single_base(
    item: Item,
    truck: TruckLoad,
    rotations: Optional[List[Tuple[float, float, float]]] = None,
) -> Optional[Placement]:
    best: Optional[Tuple[Tuple[float, float, float, float, float], Placement]] = None
    for base_idx, base in enumerate(sorted(truck.placements, key=lambda p: (p.x, p.y, p.z))):
        if not base.item.stackable:
            continue
        if base.stack_level + 1 >= base.item.max_stack:
            continue
        new_z = base.z2
        if new_z + item.h > truck.truck_type.h + 1e-9:
            continue
        top_free_rects = base.top_free_rects or [(0.0, 0.0, base.l, base.w)]
        for rx, ry, rl, rw in top_free_rects:
            for il, iw, ih in (rotations if rotations is not None else item.rotations_xy()):
                if il > rl + 1e-9 or iw > rw + 1e-9:
                    continue
                placement = Placement(
                    item=item,
                    x=base.x + rx,
                    y=base.y + ry,
                    z=new_z,
                    l=il,
                    w=iw,
                    h=ih,
                    truck_idx=-1,
                    base_item_indices=[truck.placements.index(base)],
                    stack_level=base.stack_level + 1,
                    top_free_rects=[(0.0, 0.0, il, iw)],
                )
                ok, base_indices = can_place(truck, placement.x, placement.y, placement.z, placement.l, placement.w, placement.h)
                if not ok:
                    continue
                placement.base_item_indices = base_indices
                placement.stack_level = placement_stack_level(truck, base_indices)
                score = stack_score(placement, truck)
                if best is None or score < best[0]:
                    best = (score, placement)
    return None if best is None else best[1]


def try_stack_merged_coplanar(
    item: Item,
    truck: TruckLoad,
    eps: float = 1e-4,
    rotations: Optional[List[Tuple[float, float, float]]] = None,
) -> Optional[Placement]:
    if len(truck.placements) < 2:
        return None
    grouped: Dict[float, List[Placement]] = {}
    for p in truck.placements:
        grouped.setdefault(round(p.z2, 6), []).append(p)
    best: Optional[Tuple[Tuple[float, float, float, float, float], Placement]] = None
    for z_key, parts in grouped.items():
        if len(parts) < 2:
            continue
        z_top = float(z_key)
        if z_top + item.h > truck.truck_type.h + eps:
            continue
        merged = merge_free_rectangles([(p.x, p.y, p.l, p.w) for p in parts], eps=eps)
        free_list: List[Tuple[float, float, float, float]] = list(merged)
        for q in truck.placements:
            if abs(q.z - z_top) > eps:
                continue
            next_rects: List[Tuple[float, float, float, float]] = []
            for rect in free_list:
                next_rects.extend(subtract_rect(rect, (q.x, q.y, q.l, q.w), eps))
            free_list = merge_free_rectangles(next_rects, eps=eps)
        for rx, ry, rl, rw in free_list:
            for il, iw, ih in (rotations if rotations is not None else item.rotations_xy()):
                if il > rl + eps or iw > rw + eps:
                    continue
                placement = Placement(
                    item=item,
                    x=rx,
                    y=ry,
                    z=z_top,
                    l=il,
                    w=iw,
                    h=ih,
                    truck_idx=-1,
                    base_item_indices=[],
                    stack_level=0,
                    top_free_rects=[(0.0, 0.0, il, iw)],
                )
                ok, base_indices = can_place(truck, placement.x, placement.y, placement.z, placement.l, placement.w, placement.h)
                if not ok or len(base_indices) < 2:
                    continue
                placement.base_item_indices = base_indices
                placement.stack_level = placement_stack_level(truck, base_indices)
                score = stack_score(placement, truck)
                if best is None or score < best[0]:
                    best = (score, placement)
    return None if best is None else best[1]


def stack_score(placement: Placement, truck: TruckLoad) -> Tuple[float, int, float, float, float]:
    support, bases = support_ratio(truck, placement.x, placement.y, placement.z, placement.l, placement.w)
    connected = contiguous_support_ratio(truck, placement.x, placement.y, placement.z, placement.l, placement.w)
    # Prefer stable, back-placed, low-height stacks over extending the used truck length.
    return (
        max(truck.used_length, placement.x2),
        -len(bases),
        -support - connected,
        placement.x,
        placement.y,
    )


def placement_stack_level(truck: TruckLoad, base_indices: List[int]) -> int:
    if not base_indices:
        return 0
    return 1 + max(truck.placements[idx].stack_level for idx in base_indices)


def place_item_by_rules(
    item: Item,
    truck: TruckLoad,
    remaining_items: Optional[List[Item]] = None,
    rotations: Optional[List[Tuple[float, float, float]]] = None,
) -> Optional[Placement]:
    floor_frontier_x = truck.used_length if truck.placements else None
    floor_fit = try_place_on_floor(
        item,
        truck,
        remaining_items=remaining_items,
        rotations=rotations,
        max_front_x=floor_frontier_x,
    )
    if floor_fit is not None:
        return floor_fit

    floor_expand = try_place_on_floor(
        item,
        truck,
        remaining_items=remaining_items,
        rotations=rotations,
        max_front_x=None,
    )
    if floor_expand is not None:
        return floor_expand

    rebuild_top_free_rects(truck)
    merged_stack = try_stack_merged_coplanar(item, truck, rotations=rotations)
    if merged_stack is not None:
        return merged_stack

    single_stack = try_stack_single_base(item, truck, rotations=rotations)
    if single_stack is not None:
        return single_stack
    return None


def create_new_truck(available_types: List[TruckType], item: Item) -> Optional[Tuple[int, TruckLoad]]:
    for idx, t in enumerate(available_types):
        if item.weight > t.max_weight:
            continue
        if item.h > t.h:
            continue
        if (item.l <= t.l and item.w <= t.w) or (item.w <= t.l and item.l <= t.w):
            return idx, TruckLoad(truck_type=t)
    return None


def overlap_area_xy(a: Placement, b: Placement) -> float:
    ox1, oy1 = max(a.x, b.x), max(a.y, b.y)
    ox2, oy2 = min(a.x2, b.x2), min(a.y2, b.y2)
    if ox2 <= ox1 or oy2 <= oy1:
        return 0.0
    return (ox2 - ox1) * (oy2 - oy1)


def direct_supporters(truck: TruckLoad, idx: int, z_eps: float = 1e-4) -> List[int]:
    p = truck.placements[idx]
    if p.z < z_eps:
        return []
    scored: List[Tuple[float, int]] = []
    for j, q in enumerate(truck.placements):
        if idx == j or abs(q.z2 - p.z) > z_eps:
            continue
        area = overlap_area_xy(p, q)
        if area > 1e-12:
            scored.append((-area, j))
    scored.sort()
    return [j for _, j in scored]


def floor_root_index(truck: TruckLoad, idx: int, z_eps: float = 1e-4) -> int:
    seen = set()
    cur = idx
    while True:
        if cur in seen:
            return idx
        seen.add(cur)
        p = truck.placements[cur]
        if p.z < z_eps:
            return cur
        supports = direct_supporters(truck, cur, z_eps)
        if not supports:
            return cur
        cur = supports[0]


def groups_by_floor_root(truck: TruckLoad, z_eps: float = 1e-4) -> List[List[int]]:
    groups: Dict[int, List[int]] = {}
    for i in range(len(truck.placements)):
        groups.setdefault(floor_root_index(truck, i, z_eps), []).append(i)
    return list(groups.values())


def feasible_group_shift_x(truck: TruckLoad, group: set[int], dx: float, z_eps: float = 1e-4) -> bool:
    def x_at(i: int) -> float:
        return truck.placements[i].x + (dx if i in group else 0.0)

    for i in group:
        p = truck.placements[i]
        x = x_at(i)
        if x < -1e-9 or x + p.l > truck.truck_type.l + 1e-9:
            return False
        if p.y < -1e-9 or p.y2 > truck.truck_type.w + 1e-9 or p.z < -1e-9 or p.z2 > truck.truck_type.h + 1e-9:
            return False

    for i in range(len(truck.placements)):
        for j in range(i + 1, len(truck.placements)):
            a, b = truck.placements[i], truck.placements[j]
            ax1, ax2 = x_at(i), x_at(i) + a.l
            bx1, bx2 = x_at(j), x_at(j) + b.l
            if not (ax2 <= bx1 or bx2 <= ax1 or a.y2 <= b.y or b.y2 <= a.y or a.z2 <= b.z or b.z2 <= a.z):
                return False

    for i in group:
        p = truck.placements[i]
        if p.z < z_eps:
            continue
        test = Placement(item=p.item, x=x_at(i), y=p.y, z=p.z, l=p.l, w=p.w, h=p.h, truck_idx=p.truck_idx)
        supporters: List[Tuple[float, float, float, float]] = []
        for j, q in enumerate(truck.placements):
            if i == j:
                continue
            top_z = q.z2
            if abs(top_z - p.z) > z_eps:
                continue
            qx = x_at(j)
            overlap = rect_overlap((test.x, test.y, test.l, test.w), (qx, q.y, q.l, q.w))
            if overlap is not None:
                supporters.append((qx, q.y, q.l, q.w))
        if not supporters or union_area_2d(supporters) < (p.l * p.w) - 1e-6:
            return False
    return True


def compact_truck_load(truck: TruckLoad) -> None:
    if len(truck.placements) < 2:
        return
    for _ in range(24):
        moved = False
        groups = sorted(
            groups_by_floor_root(truck),
            key=lambda g: max(truck.placements[i].x2 for i in g),
            reverse=True,
        )
        for group in groups:
            left_bound = -min(truck.placements[i].x for i in group)
            if left_bound >= -1e-9:
                continue
            lo, hi = left_bound, 0.0
            best = 0.0
            for _ in range(40):
                mid = (lo + hi) / 2.0
                if feasible_group_shift_x(truck, set(group), mid):
                    best = mid
                    hi = mid
                else:
                    lo = mid
            if best < -1e-6:
                for i in group:
                    truck.placements[i].x += best
                moved = True
        if not moved:
            break
    truck.used_length = max((p.x2 for p in truck.placements), default=0.0)
    rebuild_top_free_rects(truck)


def has_immediate_stack_child(truck: TruckLoad, idx: int, eps: float = 1e-4) -> bool:
    p = truck.placements[idx]
    for j, q in enumerate(truck.placements):
        if idx == j:
            continue
        if abs(q.z - p.z2) > eps:
            continue
        if overlap_area_xy(p, q) > 1e-12:
            return True
    return False


def upper_level_improvement_score(p: Placement) -> Tuple[float, float, float, int]:
    return (round(p.x2, 6), round(p.x, 6), round(p.z, 6), p.stack_level)


def optimize_upper_levels(truck: TruckLoad) -> None:
    # Keep the base deck fixed; only try to improve items above z=0.
    for _ in range(24):
        moved = False
        rebuild_top_free_rects(truck)
        movable_indices = [
            i for i, p in enumerate(truck.placements)
            if p.z > 1e-9 and not has_immediate_stack_child(truck, i)
        ]
        movable_indices.sort(key=lambda i: (-(truck.placements[i].x2), -truck.placements[i].z, truck.placements[i].item.item_id))
        for idx in movable_indices:
            if idx >= len(truck.placements):
                continue
            original = truck.placements[idx]
            old_score = upper_level_improvement_score(original)
            del truck.placements[idx]
            rebuild_top_free_rects(truck)
            candidate = try_stack_merged_coplanar(original.item, truck)
            if candidate is None:
                candidate = try_stack_single_base(original.item, truck)
            if candidate is None or upper_level_improvement_score(candidate) >= old_score:
                truck.placements.insert(idx, original)
                rebuild_top_free_rects(truck)
                continue
            candidate.truck_idx = original.truck_idx
            truck.placements.append(candidate)
            rebuild_top_free_rects(truck)
            moved = True
        if not moved:
            break
    compact_truck_load(truck)


def pack_once(items: List[Item], truck_types: List[TruckType], seed: int = 0) -> Tuple[List[TruckLoad], List[Item]]:
    # Heavy-first baseline, then slight seed-driven perturbation for candidate diversity.
    rng = random.Random(seed)
    sorted_items = sorted(items, key=lambda x: (-x.weight, -x.footprint, -x.max_side, x.item_id))
    for i in range(len(sorted_items) - 1):
        if rng.random() < 0.15:
            j = min(len(sorted_items) - 1, i + rng.randint(1, 4))
            sorted_items[i], sorted_items[j] = sorted_items[j], sorted_items[i]

    trucks: List[TruckLoad] = []
    unplaced: List[Item] = []
    unused_truck_types = list(truck_types)
    for item_idx, item in enumerate(sorted_items):
        remaining_items = sorted_items[item_idx + 1 :]
        rotations = item.rotations_xy()
        rng.shuffle(rotations)
        placed = False
        truck_order = list(range(len(trucks)))
        truck_order.sort(key=lambda idx: (trucks[idx].used_length, trucks[idx].current_weight))

        for idx in truck_order:
            truck = trucks[idx]
            if not truck.can_take_weight(item.weight):
                continue
            saved_free_rects = copy.deepcopy(truck.free_rects)
            placement = place_item_by_rules(
                item,
                truck,
                remaining_items=remaining_items,
                rotations=rotations,
            )
            if placement is None:
                truck.free_rects = saved_free_rects
                continue
            placement.truck_idx = idx
            truck.placements.append(placement)
            truck.current_weight += item.weight
            truck.used_length = max(truck.used_length, placement.x2)
            rebuild_top_free_rects(truck)
            placed = True
            break

        if not placed:
            new_truck_result = create_new_truck(unused_truck_types, item)
            if new_truck_result is None:
                unplaced.append(item)
                continue
            chosen_idx, new_truck = new_truck_result
            placement = place_item_by_rules(
                item,
                new_truck,
                remaining_items=remaining_items,
                rotations=rotations,
            )
            if placement is None:
                unplaced.append(item)
                continue
            placement.truck_idx = len(trucks)
            new_truck.placements.append(placement)
            new_truck.current_weight += item.weight
            new_truck.used_length = max(new_truck.used_length, placement.x2)
            rebuild_top_free_rects(new_truck)
            trucks.append(new_truck)
            unused_truck_types.pop(chosen_idx)
    for truck in trucks:
        optimize_upper_levels(truck)
        compact_truck_load(truck)
    return trucks, unplaced


def truck_floor_usable_volume(truck: TruckLoad) -> float:
    return sum(l * w * truck.truck_type.h for _, _, l, w in truck.free_rects)


def truck_air_usable_volume(truck: TruckLoad) -> float:
    air = 0.0
    for p in truck.placements:
        if not p.item.stackable:
            continue
        remaining_height = truck.truck_type.h - p.z2
        if remaining_height <= 1e-9:
            continue
        air += sum(l * w * remaining_height for _, _, l, w in p.top_free_rects)
    return air


def plan_remaining_usable_volume(trucks: List[TruckLoad]) -> Tuple[float, float, float]:
    floor_remaining = sum(truck_floor_usable_volume(t) for t in trucks)
    air_remaining = sum(truck_air_usable_volume(t) for t in trucks)
    return floor_remaining + air_remaining, floor_remaining, air_remaining


def plan_volume_usage(trucks: List[TruckLoad]) -> Tuple[float, float, float]:
    used = 0.0
    capacity = 0.0
    for t in trucks:
        used += sum(p.l * p.w * p.h for p in t.placements)
        capacity += t.truck_type.l * t.truck_type.w * t.truck_type.h
    utilization = (used / capacity) if capacity > 1e-9 else 0.0
    return used, capacity, utilization


def largest_contiguous_floor_area(truck: TruckLoad) -> float:
    if not truck.free_rects:
        return 0.0
    return max(l * w for _, _, l, w in truck.free_rects)


def largest_contiguous_air_area(truck: TruckLoad) -> float:
    best = 0.0
    for p in truck.placements:
        for _, _, l, w in p.top_free_rects:
            best = max(best, l * w)
    return best


def plan_contiguous_space_metrics(trucks: List[TruckLoad]) -> Tuple[float, float]:
    floor_best = max((largest_contiguous_floor_area(t) for t in trucks), default=0.0)
    air_best = max((largest_contiguous_air_area(t) for t in trucks), default=0.0)
    return floor_best, air_best


def plan_score(trucks: List[TruckLoad], unplaced: List[Item]) -> Tuple[int, int, float, float, float, float, float, float, float, float, float]:
    # Primary: minimize unplaced goods, then minimize truck count.
    # After that, maximize volume utilization and tie-break by compactness.
    # Tie-break with compactness/usable-space metrics.
    total_remaining, floor_remaining, air_remaining = plan_remaining_usable_volume(trucks)
    max_floor_area, max_air_area = plan_contiguous_space_metrics(trucks)
    used_volume, _capacity_volume, volume_utilization = plan_volume_usage(trucks)
    return (
        len(unplaced),
        len(trucks),
        -round(volume_utilization, 8),
        -round(used_volume, 6),
        round(total_remaining, 6),
        round(floor_remaining, 6),
        round(air_remaining, 6),
        -round(max_floor_area, 6),
        -round(max_air_area, 6),
        sum(t.used_length for t in trucks),
        sum(t.current_weight for t in trucks),
    )


def generate_candidates(
    items: List[Item], truck_types: List[TruckType], count: int = 10, attempts: int = 240
) -> List[Tuple[List[TruckLoad], List[Item]]]:
    candidates: List[Tuple[List[TruckLoad], List[Item]]] = []
    seen_signatures = set()
    total_attempts = max(1, attempts)
    for seed in range(total_attempts):
        plan, unplaced = pack_once(items, truck_types, seed=seed + 7)
        score = plan_score(plan, unplaced)
        signature = (
            score,
            tuple(
                (round(t.used_length, 3), round(t.current_weight, 1), len(t.placements))
                for t in plan
            ),
            tuple(sorted(p.item_id for p in unplaced)),
        )
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        candidates.append((copy.deepcopy(plan), copy.deepcopy(unplaced)))
    candidates.sort(key=lambda candidate: plan_score(candidate[0], candidate[1]))
    return candidates[: max(1, count)]


def print_plan_summary(plans: List[Tuple[List[TruckLoad], List[Item]]]) -> None:
    for i, (plan, unplaced) in enumerate(plans, start=1):
        score = plan_score(plan, unplaced)
        total_remaining, floor_remaining, air_remaining = plan_remaining_usable_volume(plan)
        max_floor_area, max_air_area = plan_contiguous_space_metrics(plan)
        used_volume, capacity_volume, volume_utilization = plan_volume_usage(plan)
        print(
            f"\nPlan {i}: unplaced={score[0]} trucks={score[1]} "
            f"volume_usage={used_volume:.2f}/{capacity_volume:.2f}m3 ({volume_utilization*100:.2f}%) "
            f"remaining_usable_volume={total_remaining:.2f}m3 "
            f"(floor={floor_remaining:.2f}m3 air={air_remaining:.2f}m3) "
            f"largest_contiguous_area=(floor={max_floor_area:.2f}m2 air={max_air_area:.2f}m2) "
            f"total_used_length={score[9]:.2f}m total_weight={score[10]:.1f}kg"
        )
        for t_idx, truck in enumerate(plan):
            print(
                f"  Truck {t_idx + 1} ({truck.truck_type.truck_id}): "
                f"items={len(truck.placements)} weight={truck.current_weight:.1f}kg "
                f"used_length={truck.used_length:.2f}m"
            )
            for p in sorted(truck.placements, key=lambda pl: (pl.x, pl.y, pl.z, pl.item.item_id)):
                print(
                    f"    - ID {p.item.item_id} ({p.item.name}) "
                    f"pos=({p.x:.2f},{p.y:.2f},{p.z:.2f}) "
                    f"size=({p.l:.2f}x{p.w:.2f}x{p.h:.2f}) "
                    f"weight={p.item.weight:.1f}kg level={p.stack_level}"
                )
        if unplaced:
            print("  Unplaced goods:")
            for item in sorted(unplaced, key=lambda it: (-it.weight, it.item_id)):
                print(
                    f"    - ID {item.item_id} ({item.name}) "
                    f"size=({item.l:.2f}x{item.w:.2f}x{item.h:.2f}) "
                    f"weight={item.weight:.1f}kg"
                )


def plans_to_json(plans: List[Tuple[List[TruckLoad], List[Item]]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"plans": []}
    for idx, (plan, unplaced) in enumerate(plans, start=1):
        total_remaining, floor_remaining, air_remaining = plan_remaining_usable_volume(plan)
        max_floor_area, max_air_area = plan_contiguous_space_metrics(plan)
        used_volume, capacity_volume, volume_utilization = plan_volume_usage(plan)
        trucks_out = []
        for truck in plan:
            placements = []
            for p in sorted(truck.placements, key=lambda pl: (pl.x, pl.y, pl.z, pl.item.item_id)):
                placements.append(
                    {
                        "id": p.item.item_id,
                        "name": p.item.name,
                        "weight_kg": p.item.weight,
                        "size_m": {"l": p.l, "b": p.w, "h": p.h},
                        "position_m": {"x": p.x, "y": p.y, "z": p.z},
                        "level": p.stack_level,
                        "adr": p.item.adr,
                        "adr_class": p.item.adr_class,
                    }
                )
            trucks_out.append(
                {
                    "truck_id": truck.truck_type.truck_id,
                    "truck_name": truck.truck_type.name,
                    "truck_dims_m": {"l": truck.truck_type.l, "b": truck.truck_type.w, "h": truck.truck_type.h},
                    "max_weight_kg": truck.truck_type.max_weight,
                    "weight_used_kg": truck.current_weight,
                    "used_length_m": truck.used_length,
                    "items_count": len(truck.placements),
                    "placements": placements,
                }
            )
        payload["plans"].append(
            {
                "plan_index": idx,
                "summary": {
                    "unplaced_count": len(unplaced),
                    "truck_count": len(plan),
                    "volume_used_m3": used_volume,
                    "volume_capacity_m3": capacity_volume,
                    "volume_utilization_pct": volume_utilization * 100.0,
                    "remaining_usable_volume_m3": total_remaining,
                    "remaining_floor_usable_volume_m3": floor_remaining,
                    "remaining_air_usable_volume_m3": air_remaining,
                    "largest_contiguous_floor_area_m2": max_floor_area,
                    "largest_contiguous_air_area_m2": max_air_area,
                    "total_used_length_m": sum(t.used_length for t in plan),
                    "total_weight_kg": sum(t.current_weight for t in plan),
                },
                "trucks": trucks_out,
                "unplaced_goods": [
                    {
                        "id": item.item_id,
                        "name": item.name,
                        "weight_kg": item.weight,
                        "size_m": {"l": item.l, "b": item.w, "h": item.h},
                        "adr": item.adr,
                        "adr_class": item.adr_class,
                    }
                    for item in sorted(unplaced, key=lambda it: (-it.weight, it.item_id))
                ],
            }
        )
    return payload


def plan_to_preview_dicts(plan: List[TruckLoad]) -> List[Dict[str, Any]]:
    preview_plans: List[Dict[str, Any]] = []
    for truck in plan:
        placements = []
        for p in sorted(truck.placements, key=lambda pl: (pl.x, pl.y, pl.z, pl.item.item_id)):
            placements.append(
                {
                    "ID": int(p.item.item_id),
                    "name": p.item.name,
                    "weight": p.item.weight,
                    "l": p.l,
                    "b": p.w,
                    "h": p.h,
                    "x": p.x,
                    "y": p.y,
                    "z": p.z,
                    "level": p.stack_level,
                    "adr": p.item.adr,
                    "adr_class": p.item.adr_class,
                }
            )
        preview_plans.append(
            {
                "truck": truck.truck_type.truck_id,
                "truck_dims": {"l": truck.truck_type.l, "b": truck.truck_type.w, "h": truck.truck_type.h},
                "placed_count": len(placements),
                "weight_util_pct": (truck.current_weight / truck.truck_type.max_weight * 100.0) if truck.truck_type.max_weight > 0 else 0.0,
                "placements": placements,
            }
        )
    return preview_plans


def visualize_plotly(plan: List[TruckLoad], plan_label: str = "Plan") -> None:
    import preview_3d

    preview_3d.show_load_preview(
        plan_to_preview_dicts(plan),
        use_matplotlib=False,
        title=plan_label,
    )


def visualize_matplotlib(plan: List[TruckLoad], plan_label: str = "Plan") -> None:
    import preview_3d

    preview_3d.show_load_preview(
        plan_to_preview_dicts(plan),
        use_matplotlib=True,
        title=plan_label,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate 10 truck loading arrangements with floor-first + stacking heuristics.")
    parser.add_argument("--goods", default="goods_sample.csv", help="Path to goods CSV")
    parser.add_argument("--trucks", default="trucks_sample.csv", help="Path to trucks CSV")
    parser.add_argument(
        "--input-source",
        choices=["json", "csv", "auto"],
        default="auto",
        help="Load inputs from top-level JSON payloads, CSV files, or auto (JSON when non-empty).",
    )
    parser.add_argument("--top-k", type=int, default=10, help="Number of plans to keep")
    parser.add_argument("--attempts", type=int, default=240, help="Randomized search attempts")
    parser.add_argument(
        "--json-output",
        choices=["none", "stdout", "both"],
        default="none",
        help="Emit structured JSON output to stdout as well (default: both text and JSON).",
    )
    parser.add_argument("--viz", choices=["plotly", "matplotlib", "none"], default="plotly", help="Visualization backend")
    parser.add_argument("--plan-index", type=int, default=1, help="Plan to visualize (1-based index in ranked plans)")
    parser.add_argument(
        "--preview-best-two",
        action="store_true",
        help="Automatically visualize plan 1 and 2 (if available).",
    )
    args = parser.parse_args()

    use_json = False
    if args.input_source == "json":
        use_json = True
    elif args.input_source == "auto":
        use_json = bool(GOODS_PAYLOAD.get("goods")) and bool(TRUCKS_PAYLOAD.get("trucks"))

    if use_json:
        items = parse_items_from_json(GOODS_PAYLOAD)
        trucks = parse_trucks_from_json(TRUCKS_PAYLOAD)
        if not items or not trucks:
            raise SystemExit("JSON input selected but GOODS_PAYLOAD/TRUCKS_PAYLOAD are empty.")
    else:
        items = read_goods(args.goods)
        trucks = read_trucks(args.trucks)

    plans = generate_candidates(items, trucks, count=args.top_k, attempts=args.attempts)
    if not plans:
        raise SystemExit("No loading plan could be generated.")

    if args.json_output in ("none", "both"):
        print_plan_summary(plans)
    if args.json_output in ("stdout", "both"):
        print("\nJSON output:")
        print(json.dumps(plans_to_json(plans), indent=2))
    if args.viz == "none":
        return

    # When requested, preview the two best ranked plans automatically.
    if args.preview_best_two:
        to_show = plans[: min(2, len(plans))]
        for i, (chosen, _unplaced) in enumerate(to_show, start=1):
            print(f"\nOpening visualization for Plan {i}...")
            if args.viz == "plotly":
                visualize_plotly(chosen, plan_label=f"Plan {i}")
            else:
                visualize_matplotlib(chosen, plan_label=f"Plan {i}")
        return

    idx = max(1, min(args.plan_index, len(plans))) - 1
    chosen, _unplaced = plans[idx]
    if args.viz == "plotly":
        visualize_plotly(chosen, plan_label=f"Plan {idx + 1}")
    else:
        visualize_matplotlib(chosen, plan_label=f"Plan {idx + 1}")


if __name__ == "__main__":
    main()
