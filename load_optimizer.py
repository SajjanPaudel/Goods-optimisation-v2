#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import copy
import json
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

PreOccupiedBox = Tuple[float, float, float, float, float, float]

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

# Allow small height mismatch when treating support surfaces as coplanar.
Z_COPLANAR_TOL = 1e-7

# Stacking: fraction of the candidate's footprint (l×w) that must rest on
# coplanar support (real cargo or pre-occupied synthetic placement tops).
MIN_STACK_FOOTPRINT_SUPPORT_RATIO = 0.7
# Connected component under footprint centre must cover at least this fraction
# of the footprint (reduces unstable “bridged” stacks).
MIN_STACK_CONTIGUOUS_FOOTPRINT_RATIO = 0.7
# When total footprint support meets this *stricter* threshold, allow stacking
# across disjoint support patches (e.g. cargo deck + obstacle top with a gap).
MIN_STACK_BRIDGE_TOTAL_SUPPORT_RATIO = 0.7


@dataclass
class Item:
    item_id: str
    truck_id: str
    name: str
    weight: float
    l: float
    w: float
    h: float
    stackable: bool
    max_stack: int
    adr: bool = False
    adr_class: str = ""
    adr_class_2: str = ""

    def adr_label_set(self) -> List[str]:
        # Primary + subsidiary hazard labels the item carries (non-empty, stripped).
        # Non-ADR items contribute no labels to the segregation check.
        if not self.adr:
            return []
        labels = []
        for raw in (self.adr_class, self.adr_class_2):
            token = (raw or "").strip()
            if token:
                labels.append(token)
        return labels

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
    # Each tuple is (x, y, z, l, w, h) in metres — fixed volumes cargo must not enter.
    pre_occupied_regions: Tuple[PreOccupiedBox, ...] = ()

    @property
    def has_pre_occupied(self) -> bool:
        return len(self.pre_occupied_regions) > 0

    def pre_occupied_box(self) -> Optional[PreOccupiedBox]:
        # First region only; kept for callers that assume a single obstacle.
        if not self.pre_occupied_regions:
            return None
        return self.pre_occupied_regions[0]


# ADR pairwise segregation matrix, mirrors OWN_Combined_ADRLoad.
# Values 1 and 9 mean the pair may travel together; anything else is forbidden.
AdrPair = Tuple[str, str]
AdrMatrix = Dict[AdrPair, int]
ADR_ALLOWED_VALUES: FrozenSet[int] = frozenset({1, 9})
_ADR_MATRIX_CACHE: Dict[str, AdrMatrix] = {}


def load_adr_matrix(csv_path: str | Path = "hazard.csv") -> AdrMatrix:
    key = str(Path(csv_path).resolve()) if Path(csv_path).exists() else str(csv_path)
    cached = _ADR_MATRIX_CACHE.get(key)
    if cached is not None:
        return cached
    matrix: AdrMatrix = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalise column names: the reference CSV has whitespace in headers.
        name_map = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}
        h1_key = name_map.get("hazardlabel1")
        h2_key = name_map.get("hazardlabel2")
        val_key = name_map.get("adrvalue")
        if not (h1_key and h2_key and val_key):
            raise ValueError(
                f"hazard matrix CSV at {csv_path} must contain columns "
                "Hazardlabel1, Hazardlabel2, ADRValue"
            )
        for row in reader:
            h1 = (row.get(h1_key) or "").strip()
            h2 = (row.get(h2_key) or "").strip()
            if not h1 or not h2:
                continue
            try:
                val = int((row.get(val_key) or "").strip())
            except (TypeError, ValueError):
                continue
            matrix[(h1, h2)] = val
    _ADR_MATRIX_CACHE[key] = matrix
    return matrix


def adr_pair_allowed(matrix: AdrMatrix, label1: str, label2: str) -> bool:
    # Rule 2 : An ADR pair is allowed only if the
    # matrix value is 1 or 9. Missing entries are treated as forbidden, matching
    # the original initialisation of bNotAllowed = TRUE.
    a, b = (label1 or "").strip(), (label2 or "").strip()
    if not a or not b:
        return True
    val = matrix.get((a, b))
    if val is None:
        val = matrix.get((b, a))
    if val is None:
        return False
    return val in ADR_ALLOWED_VALUES


def _classify_labels(labels: Iterable[str]) -> Tuple[bool, bool, bool, bool]:
    c1 = c52 = c42 = cother = False
    for raw in labels:
        lbl = (raw or "").strip()
        if not lbl:
            continue
        if lbl.startswith("1"):
            c1 = True
        elif lbl == "5.2":
            c52 = True
        elif lbl == "4.2":
            c42 = True
        else:
            cother = True
    return c1, c52, c42, cother


def adr_class_mix_allowed(labels: Iterable[str]) -> bool:
    # Rule 1 : Forbid (class 1 + 5.2 + other)
    # or (class 1 + 4.2 + other) on the same truck.
    c1, c52, c42, cother = _classify_labels(labels)
    if c1 and c52 and cother:
        return False
    if c1 and c42 and cother:
        return False
    return True


def adr_load_allowed(matrix: AdrMatrix, labels_on_truck: Iterable[str]) -> bool:
    # Combined check: the class-mix rule must hold AND every pairwise lookup
    # (including the diagonal) must yield an ADRValue in {1, 9}.
    labels = [lbl.strip() for lbl in labels_on_truck if lbl and lbl.strip()]
    if not adr_class_mix_allowed(labels):
        return False
    for i, a in enumerate(labels):
        for b in labels[i:]:
            if not adr_pair_allowed(matrix, a, b):
                return False
    return True


def truck_load_accepts_item_adr(
    truck: "TruckLoad",
    item: "Item",
    matrix: Optional[AdrMatrix] = None,
) -> bool:
    # Set-level compatibility: ensure adding this item's labels keeps the
    # truck's cumulative load within ADR segregation rules.
    item_labels = item.adr_label_set()
    if not item_labels:
        return True
    candidate = set(truck.adr_labels) | set(item_labels)
    if matrix is None:
        matrix = load_adr_matrix(ADR_MATRIX_PATH)
    return adr_load_allowed(matrix, candidate)


# Default path used by truck_load_accepts_item_adr when no matrix is provided.
# main() overrides this from the --hazard-matrix CLI argument.
ADR_MATRIX_PATH: str = "hazard.csv"


PRE_OCCUPIED_ITEM_ID = "__PRE_OCCUPIED__"


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
    is_pre_occupied: bool = False

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
    adr_labels: Set[str] = field(default_factory=set)

    def can_take_weight(self, weight: float) -> bool:
        return self.current_weight + weight <= self.truck_type.max_weight

    def __post_init__(self) -> None:
        if not self.free_rects:
            self.free_rects = [(0.0, 0.0, self.truck_type.l, self.truck_type.w)]
        # Register each pre-occupied volume as a synthetic placement so collision /
        # stacking sees them; carve floor footprints from free_rects when z≈0.
        for ri, (px, py, pz, pl, pw, ph) in enumerate(self.truck_type.pre_occupied_regions):
            if pz <= 1e-6:
                cut = (px, py, pl, pw)
                next_rects: List[Tuple[float, float, float, float]] = []
                for rect in self.free_rects:
                    next_rects.extend(subtract_rect(rect, cut))
                self.free_rects = merge_free_rectangles(next_rects)
            synthetic_item = Item(
                item_id=f"{PRE_OCCUPIED_ITEM_ID}:{ri}",
                truck_id=self.truck_type.truck_id,
                name=f"Pre-occupied #{ri + 1}",
                weight=0.0,
                l=pl,
                w=pw,
                h=ph,
                stackable=True,
                max_stack=10**6,
                adr=False,
                adr_class="",
                adr_class_2="",
            )
            self.placements.append(
                Placement(
                    item=synthetic_item,
                    x=px,
                    y=py,
                    z=pz,
                    l=pl,
                    w=pw,
                    h=ph,
                    truck_idx=-1,
                    base_item_indices=[],
                    stack_level=0,
                    top_free_rects=[(0.0, 0.0, pl, pw)],
                    is_pre_occupied=True,
                )
            )


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
                adr_class_2=str(row.get("adr_class_2", "") or "").strip(),
            )
        )
    return items


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    if not text:
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _parse_float_triplet(raw: Any) -> Optional[Tuple[float, float, float]]:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]))
        except (TypeError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.strip("[]()")
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (TypeError, ValueError):
        return None


def _resolve_pre_occupied_regions(row: Dict[str, Any], truck_h: float) -> Tuple[PreOccupiedBox, ...]:
    """CSV: pre_occupied_space{n}_lbh + preoccupied_position{n}_xyz; legacy l/b/h + x/y/z; JSON: pre_occupied_regions list."""
    norm = {(k or "").strip().lower(): v for k, v in row.items() if k}
    numbered: Set[int] = set()
    for key in norm:
        m = re.fullmatch(r"pre_occupied_space(\d+)_lbh", key)
        if m:
            numbered.add(int(m.group(1)))
    regions: List[PreOccupiedBox] = []
    for idx in sorted(numbered):
        lbh = _parse_float_triplet(norm.get(f"pre_occupied_space{idx}_lbh"))
        pos = None
        for pk in (f"preoccupied_position{idx}_xyz", f"pre_occupied_position{idx}_xyz"):
            pos = _parse_float_triplet(norm.get(pk))
            if pos is not None:
                break
        if lbh is None or pos is None:
            continue
        pl, pw, ph = lbh[0], lbh[1], lbh[2]
        px, py, pz = pos[0], pos[1], pos[2]
        if ph <= 1e-12:
            ph = truck_h if pl > 1e-9 and pw > 1e-9 else 0.0
        if pl > 1e-9 and pw > 1e-9 and ph > 1e-9:
            regions.append((px, py, pz, pl, pw, ph))
    if regions:
        return tuple(regions)

    pre_l = _coerce_float(norm.get("pre_occupied_space_l"))
    pre_w = _coerce_float(norm.get("pre_occupied_space_b"))
    pre_h = _coerce_float(norm.get("pre_occupied_space_h"), default=truck_h if pre_l > 0 and pre_w > 0 else 0.0)
    pre_x = _coerce_float(norm.get("preoccupied_position_x"))
    pre_y = _coerce_float(norm.get("preoccupied_position_y"))
    pre_z = _coerce_float(norm.get("preoccupied_position_z"))
    if pre_l > 1e-9 and pre_w > 1e-9 and pre_h > 1e-9:
        return ((pre_x, pre_y, pre_z, pre_l, pre_w, pre_h),)

    raw_list = row.get("pre_occupied_regions")
    if isinstance(raw_list, list) and raw_list:
        out: List[PreOccupiedBox] = []
        for ent in raw_list:
            if not isinstance(ent, dict):
                continue
            pos = ent.get("position_m") or {}
            sz = ent.get("size_m") or {}
            try:
                pl = float(sz["l"])
                pw = float(sz["b"])
                ph = float(sz.get("h", truck_h))
                px = float(pos["x"])
                py = float(pos["y"])
                pz = float(pos["z"])
            except (KeyError, TypeError, ValueError):
                continue
            if pl > 1e-9 and pw > 1e-9 and ph > 1e-9:
                out.append((px, py, pz, pl, pw, ph))
        if out:
            return tuple(out)
    return ()


def parse_trucks_from_json(payload: Dict[str, Any]) -> List[TruckType]:
    trucks: List[TruckType] = []
    for row in payload.get("trucks", []):
        truck_h = float(row["h"])
        pre_regions = _resolve_pre_occupied_regions(row, truck_h)
        trucks.append(
            TruckType(
                truck_id=str(row["id"]),
                name=str(row.get("name", "")),
                l=float(row["l"]),
                w=float(row["b"]),
                h=truck_h,
                max_weight=float(row["max_weight_kg"]),
                pre_occupied_regions=pre_regions,
            )
        )
    trucks.sort(key=lambda t: (t.l, t.w * t.h, t.max_weight), reverse=True)
    return trucks


def read_goods(path: str) -> List[Item]:
    items: List[Item] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            items.append(
                Item(
                    item_id=row["id"],
                    truck_id=row.get("truck_id", ""),
                    name=row.get("name", ""),
                    weight=float(row["weight_kg"]),
                    l=float(row["l"]),
                    w=float(row["b"]),
                    h=float(row["h"]),
                    stackable=row.get("stackable", "false").strip().lower() == "true",
                    max_stack=int(row.get("max_stack", "1") or 1),
                    adr=row.get("adr", "false").strip().lower() == "true",
                    adr_class=(row.get("adr_class", "") or "").strip(),
                    adr_class_2=(row.get("adr_class_2", "") or "").strip(),
                )
            )
    return items


def read_trucks(path: str) -> List[TruckType]:
    trucks: List[TruckType] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {(k or "").strip(): v for k, v in row.items()}
            truck_h = float(row["h"])
            pre_regions = _resolve_pre_occupied_regions(row, truck_h)
            trucks.append(
                TruckType(
                    truck_id=row["id"],
                    name=row.get("name", ""),
                    l=float(row["l"]),
                    w=float(row["b"]),
                    h=truck_h,
                    max_weight=float(row["max_weight_kg"]),
                    pre_occupied_regions=pre_regions,
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
        if abs(p.z2 - required_top) > Z_COPLANAR_TOL:
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
        if abs(p.z2 - z) > Z_COPLANAR_TOL:
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


def base_supports_weight_limit(truck: TruckLoad, base_indices: List[int], stacked_item_weight: float) -> bool:
    if not base_indices:
        return True
    # Pre-occupied bases are treated as fixed structure with effectively
    # unlimited carrying capacity, so anything sitting on them is OK.
    if any(truck.placements[idx].is_pre_occupied for idx in base_indices):
        return True
    base_weight_sum = sum(truck.placements[idx].item.weight for idx in base_indices)
    return stacked_item_weight <= base_weight_sum + 1e-9


def can_place(
    truck: TruckLoad,
    x: float,
    y: float,
    z: float,
    l: float,
    w: float,
    h: float,
    item_weight: float = 0.0,
    min_support_ratio: float = MIN_STACK_FOOTPRINT_SUPPORT_RATIO,
) -> Tuple[bool, List[int]]:
    if not within_truck(truck, x, y, z, l, w, h):
        return False, []
    for bx, by, bz, bl, bw, bh in truck.truck_type.pre_occupied_regions:
        if not (
            x + l <= bx + 1e-9
            or bx + bl <= x + 1e-9
            or y + w <= by + 1e-9
            or by + bw <= y + 1e-9
            or z + h <= bz + 1e-9
            or bz + bh <= z + 1e-9
        ):
            return False, []
    for p in truck.placements:
        if overlap_3d(p, x, y, z, l, w, h):
            return False, []
    ratio, base_indices = support_ratio(truck, x, y, z, l, w)
    if ratio < min_support_ratio:
        return False, []
    if z > 0:
        conn = contiguous_support_ratio(truck, x, y, z, l, w)
        if conn < MIN_STACK_CONTIGUOUS_FOOTPRINT_RATIO and ratio < MIN_STACK_BRIDGE_TOTAL_SUPPORT_RATIO:
            return False, []
        if not base_supports_stack(truck, base_indices):
            return False, []
        if not base_supports_weight_limit(truck, base_indices, item_weight):
            return False, []
    return True, base_indices


def candidate_positions_floor(truck: TruckLoad) -> List[Tuple[float, float, float]]:
    out: List[Tuple[float, float, float]] = []
    for x, y, _, _ in truck.free_rects:
        out.append((x, y, 0.0))
    return out


def candidate_positions_stacked(truck: TruckLoad) -> List[Tuple[float, float, float]]:
    z_levels: List[float] = []
    for p in sorted(truck.placements, key=lambda pl: pl.z2):
        if p.z2 >= truck.truck_type.h:
            continue
        if not z_levels or abs(p.z2 - z_levels[-1]) > Z_COPLANAR_TOL:
            z_levels.append(p.z2)
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
            if abs(p.z2 - z) <= Z_COPLANAR_TOL:
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
            if abs(q.z - base.z2) > Z_COPLANAR_TOL:
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


def _collect_stack_planes(truck: TruckLoad) -> List[float]:
    """Distinct horizontal support heights (placement tops) below the truck roof."""
    planes: List[float] = []
    for p in truck.placements:
        zt = p.z2
        if zt >= truck.truck_type.h - 1e-9:
            continue
        if not any(abs(zt - zp) <= Z_COPLANAR_TOL for zp in planes):
            planes.append(zt)
    return sorted(planes)


def _open_z_overlap(z_lo: float, z_hi: float, p_lo: float, p_hi: float, eps: float = 1e-6) -> bool:
    """Strict overlap in z (touching-only contacts do not count)."""
    return z_lo + eps < p_hi and p_lo + eps < z_hi


def _candidate_xy_partial_stack(
    truck: TruckLoad,
    stack_z: float,
    il: float,
    iw: float,
    ih: float,
) -> Tuple[List[float], List[float]]:
    """Discrete (x, y) origins that align item edges to support edges at ``stack_z``.

    Allows overhang / air gap under part of the footprint; feasibility is still
    enforced by ``can_place`` (footprint support ratio and contiguous rules).

    Seeds include coplanar support tops at ``stack_z``, plus footprint edges from
    any placement whose 3D bulk overlaps the candidate slice ``[stack_z, stack_z+ih)``
    (e.g. narrow lanes between two pallets at the same deck height).
    """
    L = truck.truck_type.l
    W = truck.truck_type.w
    if il > L + 1e-9 or iw > W + 1e-9:
        return [], []
    hi_x = max(0.0, L - il)
    hi_y = max(0.0, W - iw)
    xs: Set[float] = {0.0, hi_x}
    ys: Set[float] = {0.0, hi_y}

    def add_edges_from_placement(p: Placement) -> None:
        for xv in (p.x, p.x2 - il, p.x2):
            xs.add(max(0.0, min(float(xv), hi_x)))
        for yv in (p.y, p.y2 - iw, p.y2):
            ys.add(max(0.0, min(float(yv), hi_y)))

    for p in truck.placements:
        if abs(p.z2 - stack_z) <= Z_COPLANAR_TOL:
            add_edges_from_placement(p)
    slice_hi = stack_z + ih
    for p in truck.placements:
        if abs(p.z2 - stack_z) <= Z_COPLANAR_TOL:
            continue
        if _open_z_overlap(stack_z, slice_hi, p.z, p.z2):
            add_edges_from_placement(p)
    return sorted(xs), sorted(ys)


def try_stack_partial_support(
    item: Item,
    truck: TruckLoad,
    rotations: Optional[List[Tuple[float, float, float]]] = None,
    plane_filter: Optional[Any] = None,
) -> Optional[Placement]:
    """Stack with possible overhang if coplanar support still meets ratio rules.

    Unlike ``try_stack_single_base``, the footprint need not fit inside one
    ``top_free_rect``; support may come from several adjacent tops or a subset
    of the obstacle deck with deliberate air gap, as long as ``can_place`` accepts it.

    If ``plane_filter`` is set, only support heights ``stack_z`` for which
    ``plane_filter(stack_z)`` is true are considered (e.g. pre-occupied deck only).
    """
    rots = rotations if rotations is not None else item.rotations_xy()
    best: Optional[Tuple[Tuple[float, float, float, float, float], Placement]] = None
    for stack_z in _collect_stack_planes(truck):
        if plane_filter is not None and not plane_filter(stack_z):
            continue
        for il, iw, ih in rots:
            if stack_z + ih > truck.truck_type.h + 1e-9:
                continue
            xs, ys = _candidate_xy_partial_stack(truck, stack_z, il, iw, ih)
            if not xs or not ys:
                continue
            for x in xs:
                for y in ys:
                    ok, base_indices = can_place(
                        truck,
                        x,
                        y,
                        stack_z,
                        il,
                        iw,
                        ih,
                        item_weight=item.weight,
                    )
                    if not ok:
                        continue
                    placement = Placement(
                        item=item,
                        x=x,
                        y=y,
                        z=stack_z,
                        l=il,
                        w=iw,
                        h=ih,
                        truck_idx=-1,
                        base_item_indices=base_indices,
                        stack_level=placement_stack_level(truck, base_indices),
                        top_free_rects=[(0.0, 0.0, il, iw)],
                    )
                    score = stack_score(placement, truck)
                    if best is None or score < best[0]:
                        best = (score, placement)
    return None if best is None else best[1]


def try_stack_single_base(
    item: Item,
    truck: TruckLoad,
    rotations: Optional[List[Tuple[float, float, float]]] = None,
    base_filter: Optional[Any] = None,
) -> Optional[Placement]:
    best: Optional[Tuple[Tuple[float, float, float, float, float], Placement]] = None
    for base_idx, base in enumerate(sorted(truck.placements, key=lambda p: (p.x, p.y, p.z))):
        if base_filter is not None and not base_filter(base):
            continue
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
                ok, base_indices = can_place(
                    truck,
                    placement.x,
                    placement.y,
                    placement.z,
                    placement.l,
                    placement.w,
                    placement.h,
                    item_weight=placement.item.weight,
                )
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
    grouped: Dict[int, List[Placement]] = {}
    for p in truck.placements:
        bucket = int(round(p.z2 / Z_COPLANAR_TOL)) if Z_COPLANAR_TOL > 0 else int(round(p.z2 * 1e6))
        grouped.setdefault(bucket, []).append(p)
    best: Optional[Tuple[Tuple[float, float, float, float, float], Placement]] = None
    for _z_key, parts in grouped.items():
        if len(parts) < 2:
            continue
        z_top = sum(p.z2 for p in parts) / len(parts)
        if z_top + item.h > truck.truck_type.h + eps:
            continue
        merged = merge_free_rectangles([(p.x, p.y, p.l, p.w) for p in parts], eps=eps)
        free_list: List[Tuple[float, float, float, float]] = list(merged)
        for q in truck.placements:
            if abs(q.z - z_top) > Z_COPLANAR_TOL:
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
                ok, base_indices = can_place(
                    truck,
                    placement.x,
                    placement.y,
                    placement.z,
                    placement.l,
                    placement.w,
                    placement.h,
                    item_weight=placement.item.weight,
                )
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


def _front_pre_void_boxes(tt: TruckType, eps: float = 1e-6) -> List[PreOccupiedBox]:
    """Regions whose footprint starts at the nose (low x): deck-on-void is usable forward space."""
    return [b for b in tt.pre_occupied_regions if b[0] <= eps]


def _partial_plane_over_front_voids(truck: TruckLoad, front_boxes: List[PreOccupiedBox]) -> Any:
    """Coplanar with any nose-void deck top, or with cargo tops overlapping any nose void XY footprint."""

    deck_heights = [pz + ph for _px, _py, pz, _pl, _pw, ph in front_boxes]

    def ok(sz: float) -> bool:
        for dh in deck_heights:
            if abs(sz - dh) <= Z_COPLANAR_TOL:
                return True
        for px, py, _pz, pl, pw, _ph in front_boxes:
            vx1, vy1, vx2, vy2 = px, py, px + pl, py + pw
            for p in truck.placements:
                if abs(p.z2 - sz) > Z_COPLANAR_TOL:
                    continue
                if p.x < vx2 - 1e-9 and p.x2 > vx1 + 1e-9 and p.y < vy2 - 1e-9 and p.y2 > vy1 + 1e-9:
                    return True
        return False

    return ok


def place_item_by_rules(
    item: Item,
    truck: TruckLoad,
    remaining_items: Optional[List[Item]] = None,
    rotations: Optional[List[Tuple[float, float, float]]] = None,
) -> Optional[Placement]:
    rebuild_top_free_rects(truck)

    tt = truck.truck_type
    front_boxes = _front_pre_void_boxes(tt)
    front_void = bool(front_boxes)

    # Irregular bed void at the nose: usable deck is on top of the fixed volume.
    # Offer that surface before jumping to open floor behind the void (high x).
    if front_void:
        pre_top = try_stack_single_base(
            item,
            truck,
            rotations=rotations,
            base_filter=lambda base: base.is_pre_occupied,
        )
        if pre_top is not None:
            return pre_top

        partial_stack = try_stack_partial_support(
            item,
            truck,
            rotations=rotations,
            plane_filter=_partial_plane_over_front_voids(truck, front_boxes),
        )
        if partial_stack is not None:
            return partial_stack

    # Open floor along usable length (front to back where floor exists).
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

    # Rear / inset void: deck-over-fixed-volume only after floor + normal stacks.
    if not front_void:
        pre_top = try_stack_single_base(
            item,
            truck,
            rotations=rotations,
            base_filter=lambda base: base.is_pre_occupied,
        )
        if pre_top is not None:
            return pre_top

        partial_stack = try_stack_partial_support(item, truck, rotations=rotations)
        if partial_stack is not None:
            return partial_stack
    return None


def create_new_truck(available_types: List[TruckType], item: Item) -> Optional[Tuple[int, TruckLoad]]:
    compatible: List[Tuple[int, TruckType]] = []
    for idx, t in enumerate(available_types):
        if item.weight > t.max_weight:
            continue
        if item.h > t.h:
            continue
        if (item.l <= t.l and item.w <= t.w) or (item.w <= t.l and item.l <= t.w):
            compatible.append((idx, t))
    if not compatible:
        return None

    # Prefer the largest sufficient unused type so loads consolidate into fewer
    # trucks (aligned with plan_score: minimize unplaced, then truck count).
    # Tie-break length first so long goods (e.g. pipe) land on a deck that can
    # host them together with the rest of the shipment when possible.
    best_idx, best_truck = min(
        compatible,
        key=lambda it: (
            it[1].l,
            it[1].w * it[1].h,
            it[1].l * it[1].w * it[1].h,
            it[1].max_weight,
        ),
    )
    return best_idx, TruckLoad(truck_type=best_truck)


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
        if idx == j or abs(q.z2 - p.z) > Z_COPLANAR_TOL:
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
            if abs(top_z - p.z) > Z_COPLANAR_TOL:
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
            if any(truck.placements[i].is_pre_occupied for i in group):
                continue
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
        if abs(q.z - p.z2) > Z_COPLANAR_TOL:
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

        if trucks:
            choice = _best_placement_on_existing_trucks_by_volume(
                item, trucks, remaining_items, rotations
            )
            if choice is not None:
                idx, placement, free_snap = choice
                placement.truck_idx = idx
                tgt = trucks[idx]
                tgt.placements.append(placement)
                tgt.free_rects = free_snap
                tgt.current_weight += item.weight
                tgt.used_length = max(tgt.used_length, placement.x2)
                tgt.adr_labels.update(item.adr_label_set())
                rebuild_top_free_rects(tgt)
                placed = True

        if not placed:
            opened = _best_placement_new_truck_by_volume(
                item, trucks, unused_truck_types, remaining_items, rotations
            )
            if opened is None:
                unplaced.append(item)
                continue
            chosen_idx, new_truck = opened
            trucks.append(new_truck)
            unused_truck_types.pop(chosen_idx)

    # Heavy-first ordering tries light “bridge” loads before pallets/obstacles exist,
    # then never revisits them. One retry wave often fits full-length pipes in narrow
    # y-lanes once surround cargo is down (remaining_items=[] avoids lookahead penalties).
    if unplaced and trucks:
        retry_batch = list(unplaced)
        unplaced.clear()
        retry_need_len = _min_interior_length_for_pending_items(retry_batch)
        for item in retry_batch:
            rotations = item.rotations_xy()
            rng.shuffle(rotations)
            pending_rest = [it for it in retry_batch if it is not item]
            choice = _best_placement_on_existing_trucks_by_volume(
                item,
                trucks,
                pending_rest,
                rotations,
            )
            if choice is None:
                unplaced.append(item)
                continue
            idx, placement, free_snap = choice
            placement.truck_idx = idx
            tgt = trucks[idx]
            tgt.placements.append(placement)
            tgt.free_rects = free_snap
            tgt.current_weight += item.weight
            tgt.used_length = max(tgt.used_length, placement.x2)
            tgt.adr_labels.update(item.adr_label_set())
            rebuild_top_free_rects(tgt)

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
        used += sum(p.l * p.w * p.h for p in t.placements if not p.is_pre_occupied)
        pre_volume = sum(
            p.l * p.w * p.h for p in t.placements if p.is_pre_occupied
        )
        capacity += t.truck_type.l * t.truck_type.w * t.truck_type.h - pre_volume
    utilization = (used / capacity) if capacity > 1e-9 else 0.0
    return used, capacity, utilization


def _min_interior_length_for_pending_items(items: List[Item]) -> float:
    """Conservative minimum trailer length (x) needed if each piece spans along +x up to its longest axis."""
    if not items:
        return 0.0
    return max(max(it.l, it.w, it.h) for it in items)


def _volume_based_placement_score(trucks: List[TruckLoad], dest_idx: int) -> Tuple[float, float, float, float]:
    """Sort key — larger is better.

    Prefer plans with higher *overall* volume utilization, then more cargo placed,
    then longer/larger decks so consolidation stays feasible (avoid rewarding only V/C
    on the smallest compatible trailer).
    """
    _, _, util_plan = plan_volume_usage(trucks)
    tt = trucks[dest_idx].truck_type
    used_total = sum(
        sum(p.l * p.w * p.h for p in t.placements if not p.is_pre_occupied)
        for t in trucks
    )
    return (util_plan, used_total, tt.l, tt.w * tt.h)


def _hypothesis_apply_placement(
    trucks: List[TruckLoad],
    dest_idx: int,
    placement: Placement,
    item: Item,
) -> List[TruckLoad]:
    hypo = copy.deepcopy(trucks)
    pl = copy.deepcopy(placement)
    pl.truck_idx = dest_idx
    tgt = hypo[dest_idx]
    tgt.placements.append(pl)
    tgt.current_weight += item.weight
    tgt.used_length = max(tgt.used_length, pl.x2)
    tgt.adr_labels.update(item.adr_label_set())
    rebuild_top_free_rects(tgt)
    return hypo


def _best_placement_on_existing_trucks_by_volume(
    item: Item,
    trucks: List[TruckLoad],
    remaining_items: List[Item],
    rotations: List[Tuple[float, float, float]],
) -> Optional[Tuple[int, Placement, List[Tuple[float, float, float, float]]]]:
    """Evaluate every truck that can legally accept the item; pick best volume score.

    Returns ``(idx, placement, free_rects_snapshot)`` where ``free_rects_snapshot`` must be
    copied onto the real truck after append — ``try_place_on_floor`` mutates free rects
    during search without adding the placement yet.
    """
    best: Optional[Tuple[Tuple[float, float, float, float], int, Placement, List[Tuple[float, float, float, float]]]] = None
    need_len = _min_interior_length_for_pending_items([item] + remaining_items)
    for idx in range(len(trucks)):
        truck = trucks[idx]
        if truck.truck_type.l + 1e-9 < need_len:
            continue
        if not truck.can_take_weight(item.weight):
            continue
        if not truck_load_accepts_item_adr(truck, item):
            continue
        trial = copy.deepcopy(truck)
        placement = place_item_by_rules(
            item,
            trial,
            remaining_items=remaining_items,
            rotations=rotations,
        )
        if placement is None:
            continue
        free_snap = copy.deepcopy(trial.free_rects)
        hypo = _hypothesis_apply_placement(trucks, idx, placement, item)
        score = _volume_based_placement_score(hypo, idx)
        if best is None or score > best[0]:
            best = (score, idx, placement, free_snap)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _best_placement_new_truck_by_volume(
    item: Item,
    trucks: List[TruckLoad],
    unused_truck_types: List[TruckType],
    remaining_items: List[Item],
    rotations: List[Tuple[float, float, float]],
) -> Optional[Tuple[int, TruckLoad]]:
    """Try each unused compatible truck type; return best-scoring committed ``TruckLoad``."""
    best: Optional[Tuple[Tuple[float, float, float, float], int, TruckLoad]] = None
    need_len = _min_interior_length_for_pending_items([item] + remaining_items)
    for ui, tt in enumerate(unused_truck_types):
        if tt.l + 1e-9 < need_len:
            continue
        if item.weight > tt.max_weight:
            continue
        if item.h > tt.h:
            continue
        if not ((item.l <= tt.l and item.w <= tt.w) or (item.w <= tt.l and item.l <= tt.w)):
            continue
        trial = TruckLoad(truck_type=tt)
        if not truck_load_accepts_item_adr(trial, item):
            continue
        placement = place_item_by_rules(
            item,
            trial,
            remaining_items=remaining_items,
            rotations=rotations,
        )
        if placement is None:
            continue
        placement.truck_idx = len(trucks)
        trial.placements.append(placement)
        trial.current_weight += item.weight
        trial.used_length = max(trial.used_length, placement.x2)
        trial.adr_labels.update(item.adr_label_set())
        rebuild_top_free_rects(trial)
        hypo = copy.deepcopy(trucks)
        hypo.append(copy.deepcopy(trial))
        dest_idx = len(hypo) - 1
        score = _volume_based_placement_score(hypo, dest_idx)
        if best is None or score > best[0]:
            best = (score, ui, copy.deepcopy(trial))
    if best is None:
        return None
    return best[1], best[2]


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

def truck_segment_weights(truck: TruckLoad, n: int) -> List[float]:
    """Split each item's weight proportionally across n equal-length x-bins."""
    n = max(1, int(n))
    L = truck.truck_type.l
    seg = L / n
    weights = [0.0] * n
    for p in truck.placements:
        if p.l <= 0 or seg <= 0:
            continue
        for i in range(n):
            lo = i * seg
            hi = L if i == n - 1 else (i + 1) * seg
            span = max(0.0, min(p.x2, hi) - max(p.x, lo))
            if span <= 0:
                continue
            weights[i] += p.item.weight * (span / p.l)
    return weights

def truck_front_back_weights(truck: TruckLoad) -> Tuple[float, float]:
    a, b = truck_segment_weights(truck, 2)
    return a, b


def format_plan_weight_balance(plan: List[TruckLoad], segments: int = 2) -> str:
    parts: List[str] = []
    labels = _segment_labels(segments)  # ["Front", "Back"], or ["Seg1", "Seg2", "Seg3"], etc.
    for truck in plan:
        ws = truck_segment_weights(truck, segments)
        body = " ".join(f"{lbl} {w:.0f}kg" for lbl, w in zip(labels, ws))
        parts.append(f"{truck.truck_type.truck_id} {body}")
    return " | ".join(parts)


def _segment_labels(n: int) -> List[str]:
    if n == 2:
        return ["Front", "Back"]
    if n == 3:
        return ["Front", "Mid", "Back"]
    return [f"Seg{i+1}" for i in range(n)]

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
            real_placements = [p for p in truck.placements if not p.is_pre_occupied]
            print(
                f"  Truck {t_idx + 1} ({truck.truck_type.truck_id}): "
                f"items={len(real_placements)} weight={truck.current_weight:.1f}kg "
                f"used_length={truck.used_length:.2f}m"
            )
            for p in sorted(real_placements, key=lambda pl: (pl.x, pl.y, pl.z, pl.item.item_id)):
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


def plans_to_json(
    plans: List[Tuple[List[TruckLoad], List[Item]]],
    segments: int = 2,
) -> Dict[str, Any]:
    seg_n = max(1, int(segments))
    seg_labels = _segment_labels(seg_n)
    payload: Dict[str, Any] = {"plans": []}
    for idx, (plan, unplaced) in enumerate(plans, start=1):
        total_remaining, floor_remaining, air_remaining = plan_remaining_usable_volume(plan)
        max_floor_area, max_air_area = plan_contiguous_space_metrics(plan)
        used_volume, capacity_volume, volume_utilization = plan_volume_usage(plan)
        trucks_out = []
        for truck in plan:
            placements = []
            real_placements = [p for p in truck.placements if not p.is_pre_occupied]
            for p in sorted(real_placements, key=lambda pl: (pl.x, pl.y, pl.z, pl.item.item_id)):
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
                        "adr_class_2": p.item.adr_class_2,
                    }
                )
            seg_weights = truck_segment_weights(truck, seg_n)
            seg_length = truck.truck_type.l / seg_n if seg_n > 0 else 0.0
            po_regions = truck.truck_type.pre_occupied_regions
            trucks_out.append(
                {
                    "truck_id": truck.truck_type.truck_id,
                    "truck_name": truck.truck_type.name,
                    "truck_dims_m": {"l": truck.truck_type.l, "b": truck.truck_type.w, "h": truck.truck_type.h},
                    "max_weight_kg": truck.truck_type.max_weight,
                    "weight_used_kg": truck.current_weight,
                    "used_length_m": truck.used_length,
                    "items_count": len(real_placements),
                    "pre_occupied_regions": [
                        {
                            "position_m": {"x": px, "y": py, "z": pz},
                            "size_m": {"l": pl, "b": pw, "h": ph},
                            "volume_m3": pl * pw * ph,
                        }
                        for (px, py, pz, pl, pw, ph) in po_regions
                    ],
                    "adr_labels_loaded": sorted(truck.adr_labels),
                    "weight_segments": [
                        {
                            "label": label,
                            "x_start_m": i * seg_length,
                            "x_end_m": truck.truck_type.l if i == seg_n - 1 else (i + 1) * seg_length,
                            "weight_kg": w,
                        }
                        for i, (label, w) in enumerate(zip(seg_labels, seg_weights))
                    ],
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
                        "adr_class_2": item.adr_class_2,
                    }
                    for item in sorted(unplaced, key=lambda it: (-it.weight, it.item_id))
                ],
            }
        )
    return payload


def plan_to_preview_dicts(plan: List[TruckLoad], segments: int = 2) -> List[Dict[str, Any]]:
    seg_n = max(1, int(segments))
    seg_labels = _segment_labels(seg_n)
    preview_plans: List[Dict[str, Any]] = []
    for truck in plan:
        placements = []
        real_placements = [p for p in truck.placements if not p.is_pre_occupied]
        for p in sorted(real_placements, key=lambda pl: (pl.x, pl.y, pl.z, pl.item.item_id)):
            placements.append(
                {
                    "ID": int(p.item.item_id),
                    "truck_id": p.item.truck_id,
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
                    "adr_class_2": p.item.adr_class_2,
                }
            )
        segment_weights = truck_segment_weights(truck, seg_n)
        plan_dict: Dict[str, Any] = {
            "truck": truck.truck_type.truck_id,
            "truck_dims": {"l": truck.truck_type.l, "b": truck.truck_type.w, "h": truck.truck_type.h},
            "placed_count": len(placements),
            "weight_util_pct": (truck.current_weight / truck.truck_type.max_weight * 100.0) if truck.truck_type.max_weight > 0 else 0.0,
            "segment_weights_kg": segment_weights,
            "segment_labels": seg_labels,
            "segment_count": len(segment_weights),
            "placements": placements,
        }
        regions = truck.truck_type.pre_occupied_regions
        if regions:
            plan_dict["pre_occupied_regions"] = [
                {"x": px, "y": py, "z": pz, "l": pl, "b": pw, "h": ph}
                for (px, py, pz, pl, pw, ph) in regions
            ]
            px, py, pz, pl, pw, ph = regions[0]
            plan_dict["pre_occupied"] = {"x": px, "y": py, "z": pz, "l": pl, "b": pw, "h": ph}
        preview_plans.append(plan_dict)
    return preview_plans


def plan_preview_signature(plan: List[TruckLoad]) -> Tuple[Any, ...]:
    """Stable signature for deduplicating visually identical plans."""
    truck_sigs: List[Tuple[Any, ...]] = []
    for truck in plan:
        placements = tuple(
            sorted(
                (
                    p.item.item_id,
                    round(p.x, 4),
                    round(p.y, 4),
                    round(p.z, 4),
                    round(p.l, 4),
                    round(p.w, 4),
                    round(p.h, 4),
                )
                for p in truck.placements
            )
        )
        truck_sigs.append((truck.truck_type.truck_id, placements))
    return tuple(truck_sigs)


def visualize_plotly(plan: List[TruckLoad], plan_label: str = "Plan", segments: int = 2) -> None:
    import preview_3d

    preview_3d.show_load_preview(
        plan_to_preview_dicts(plan, segments=segments),
        use_matplotlib=False,
        title=plan_label,
    )


def visualize_matplotlib(plan: List[TruckLoad], plan_label: str = "Plan", segments: int = 2) -> None:
    import preview_3d

    preview_3d.show_load_preview(
        plan_to_preview_dicts(plan, segments=segments),
        use_matplotlib=True,
        title=plan_label,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate 10 truck loading arrangements with floor-first + stacking heuristics.")
    parser.add_argument("--goods", default="goods_sample.csv", help="Path to goods CSV")
    parser.add_argument("--trucks", default="trucks_sample.csv", help="Path to trucks CSV")
    parser.add_argument(
        "--hazard-matrix",
        default="hazard.csv",
        help="Path to the ADR segregation matrix CSV (Hazardlabel1,Hazardlabel2,ADRValue).",
    )
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
    parser.add_argument(
        "--weight-segments",
        type=int,
        default=2,
        help="Number of equal-length x-axis bins to report per-truck weight totals for (default: 2 = front/back).",
    )
    args = parser.parse_args()

    global ADR_MATRIX_PATH
    ADR_MATRIX_PATH = args.hazard_matrix
    if Path(args.hazard_matrix).exists():
        load_adr_matrix(args.hazard_matrix)

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

    seg_n = max(1, int(args.weight_segments))

    if args.json_output in ("none", "both"):
        print_plan_summary(plans)
    if args.json_output in ("stdout", "both"):
        print("\nJSON output:")
        print(json.dumps(plans_to_json(plans, segments=seg_n), indent=2))
    if args.viz == "none":
        return

    # Auto-preview distinct plans when top-k > 1.
    # Legacy support: --preview-best-two still forces a two-plan window.
    if args.top_k > 1 or args.preview_best_two:
        limit = min(2, len(plans)) if args.preview_best_two else min(args.top_k, len(plans))
        seen = set()
        distinct_to_show: List[Tuple[int, List[TruckLoad]]] = []
        for rank, (candidate_plan, _unplaced) in enumerate(plans[:limit], start=1):
            sig = plan_preview_signature(candidate_plan)
            if sig in seen:
                continue
            seen.add(sig)
            distinct_to_show.append((rank, candidate_plan))

        print(f"\nOpening {len(distinct_to_show)} distinct visualization(s) from top {limit} plan(s)...")
        for rank, chosen in distinct_to_show:
            balance = format_plan_weight_balance(chosen, segments=seg_n)
            print(f"  - Opening visualization for Plan {rank}... {balance}")
            if args.viz == "plotly":
                visualize_plotly(chosen, plan_label=f"Plan {rank}", segments=seg_n)
            else:
                visualize_matplotlib(chosen, plan_label=f"Plan {rank}", segments=seg_n)
        return

    idx = max(1, min(args.plan_index, len(plans))) - 1
    chosen, _unplaced = plans[idx]
    balance = format_plan_weight_balance(chosen, segments=seg_n)
    print(f"  - Opening visualization for Plan {idx + 1}... {balance}")
    if args.viz == "plotly":
        visualize_plotly(chosen, plan_label=f"Plan {idx + 1}", segments=seg_n)
    else:
        visualize_matplotlib(chosen, plan_label=f"Plan {idx + 1}", segments=seg_n)


if __name__ == "__main__":
    main()
