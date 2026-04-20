# Load Optimizer

`load_optimizer.py` takes a list of **goods** (boxes, pallets, pipes, etc.) and a list of
available **trucks**, and produces the top‑K loading plans: which items go on which truck,
at which exact `(x, y, z)` position, in which rotation, and at which stacking level.

It is a 3D bin‑packing heuristic built around three ideas:

1. **Heavy / long items first** – hardest to fit, so they drive truck choice.
2. **Floor first, stack second** – never stack until the floor is committed.
3. **Pick the smallest truck that still fits** – so we don’t waste a 15 m trailer on a 5 m load.

---

## 1. Inputs

Two CSVs (or equivalent JSON payloads). Samples in the repo:

### `goods_sample.csv`

```csv
id,truck_id,name,weight_kg,l,b,h,stackable,max_stack,adr,adr_class
1,XXXXX1,Rør,600.00,12.00,0.20,0.20,true,1,false,
2,XXXXX2,Pall,300.00,5.25,0.80,0.40,true,1,false,
3,XXXXX3,Pall (SBR),260.00,3.30,0.80,0.65,true,1,false,
4,XXXXX4,Pall (SBR),220.00,3.30,0.80,0.60,true,1,false,
5,XXXXX5,PAll m/ 2 x gassflasker (SBR),60.00,1.20,0.80,0.35,true,1,false,
```


| Field       | Meaning                                                 |
| ----------- | ------------------------------------------------------- |
| `l, b, h`   | Length × breadth × height in meters                     |
| `weight_kg` | Weight of the item                                      |
| `stackable` | Can anything be placed on top of it?                    |
| `max_stack` | How many levels high this item can support above itself |
| `adr`       | Dangerous goods flag (reserved for truck filtering)     |


### `trucks_sample.csv`

```csv
id,name,l,b,h,max_weight_kg,...
BV2727,Bil BY2727 10x2.5m,10,2.5,2.5,60000,...
EW2068,Bil EW2068 15x2.5m,15,2.5,2.5,20000,...
RX5307,Bil RX5307 18x3.5m,18,3.5,2.8,18000,...
...
```

Trucks define the internal bed dimensions (`l × b × h`) and a weight ceiling.

---

## 2. Hard constraints

Every placement must satisfy all of these, or it is rejected:

1. **Inside the truck:** `0 ≤ x, x+l ≤ truck.l` (same for `y, z`).
2. **No overlap:** the 3D box of the new item cannot intersect any already‑placed box
  (`overlap_3d`).
3. **Weight:** `truck.current_weight + item.weight ≤ truck.max_weight`.
4. **Rotations:** only 90° rotations on the floor plane. Height axis stays vertical —
  pallets are not tipped over. If `l == w` only one rotation is tried.
   See `Item.rotations_xy`.
5. **Support ratio ≥ 98%** when `z > 0`: the item’s footprint must sit on tops of items
  whose top surface is exactly at `z` (within `Z_COPLANAR_TOL`). Computed by
   `support_ratio` via a true union area, not a simple sum.
6. **Contiguous support ≥ 75%:** the supporting footprint must form one connected region
  through the item’s center. Two small disjoint supports under the corners are rejected
   (`contiguous_support_ratio`). This mimics how a real forklift driver thinks.
7. **Base is stackable:** every base item underneath must have `stackable=True` and
  `base.stack_level + 1 < base.max_stack` (`base_supports_stack`).
8. **Weight rule for stacks:** the stacked item’s weight must be
  `≤ sum of weights of the items it sits on` (`base_supports_weight_limit`). A 500 kg
   crate cannot be placed on a 60 kg pallet.

All eight are enforced in `can_place()`.

---

## 3. How a truck is chosen

Two moments matter: **sorting the candidate trucks** and **opening a new truck**.

### 3.1 Initial sort (`read_trucks` / `parse_trucks_from_json`)

Trucks are sorted longest‑first, then by floor area × height, then by payload:

```python
trucks.sort(key=lambda t: (t.l, t.w * t.h, t.max_weight), reverse=True)
```

This is only the display/iteration order — it is **not** the choice. Longest‑first helps
because a 12 m pipe has no chance on a 7 m truck, so we look at long trucks first when
asking "is there any truck this item could ever fit on?".

### 3.2 Opening a new truck (`create_new_truck`)

When an item cannot fit into any already‑opened truck, the optimizer opens a fresh one.
It filters `available_types` to those that are physically compatible:

- `item.weight ≤ truck.max_weight`
- `item.h ≤ truck.h`
- The item footprint fits in either rotation: `(l ≤ t.l and w ≤ t.w)` or `(w ≤ t.l and l ≤ t.w)`

Among compatible trucks, it then picks the **smallest sufficient** one:

```python
key = (t.l * t.w * t.h,   # 1. smallest volume
       t.max_weight,      # 2. smallest payload
       t.l * t.w,          # 3. smallest floor area
       t.l)                # 4. shortest length
```

### Example — the 12 m pipe

Looking at `goods_sample.csv`, item `1` is a 12.00 × 0.20 × 0.20 m pipe at 600 kg.

Walking the truck list:


| Truck  | L×B×H      | Reason it’s rejected / accepted          |
| ------ | ---------- | ---------------------------------------- |
| UL1521 | 6×2.5×2.5  | Rejected: 12 m pipe doesn’t fit (6 < 12) |
| EX7870 | 7×2.5×2.5  | Rejected                                 |
| BV2727 | 10×2.5×2.5 | Rejected                                 |
| VC8543 | 12×2.5×2.5 | **Accepted** (exactly 12 m)              |
| VC8544 | 13×2.5×2.5 | Accepted, larger                         |
| EX3726 | 14×2.5×2.5 | Accepted, larger                         |
| RX5307 | 18×3.5×2.8 | Accepted, much larger                    |


After filtering, the `min(key=...)` rule picks `VC8543` because it has the smallest volume
(12×2.5×2.5 = 75 m³) among those that fit. Had `VC8543` not existed, `VC8544` would be
next (81.25 m³), and so on.

---

## 4. How a placement is chosen

For each item (processed heavy-first with some randomized perturbation), the optimizer
tries four strategies in order — first success wins (`place_item_by_rules`):

### Step 1 – Floor, within current frontier

The optimizer tracks `truck.used_length` = max `x2` of any placed item. It first tries to
slot the new item **behind** that frontier (`max_front_x=truck.used_length`), so the truck
doesn’t get longer than it already is. This is what keeps loads compact.

### Step 2 – Floor, extending the frontier

If Step 1 fails, the same routine is run again without the frontier limit. The item gets
a new spot further forward.

Both floor attempts use `try_place_on_floor`, which iterates every **free rectangle**
(maintained by a Guillotine free‑rectangle list), tries every rotation, and picks the
best‑scoring `(x, y)`.

### Step 3 – Stacking on a merged coplanar base

If the floor is full, `try_stack_merged_coplanar` looks for two or more existing items
whose tops share the same `z` and whose top surfaces form a single platform wide enough
for the new item. This is how two short pallets can hold a long pallet on top.

### Step 4 – Stacking on a single base

Last resort: `try_stack_single_base` puts the item fully on top of one base pallet.

---

## 5. The placement scoring function

Both floor and stack passes use a tie‑breaker tuple, picked with `min(...)`. Lower is
better.

### 5.1 Floor score (`try_place_on_floor`)

```python
score = (
    fragmentation_penalty,  # 1. don't ruin future long/flat slots
    rx,                     # 2. push toward the back of the trailer
    ry,                     # 3. push toward the side wall
    rx + il,                # 4. keep used_length small
    rl * rw - il * iw,      # 5. leave the least wasted rect
    -iw,                    # 6. prefer the wider orientation
)
```

**Fragmentation penalty (`reserve_fragmentation_penalty`).** This is the key anti‑greedy
signal. Before committing a placement, the optimizer simulates the guillotine split and
asks: *"how much area would I lose for long/flat items still waiting?"* A long/flat item
is one whose longest side ≥ 1.8 m and whose height ≤ 0.35 m (`Item.is_long_flat`). If
placing this pallet would break the only strip where a 5.25 m pallet could still fit, the
penalty shoots up and a different position wins.

### Example — why item `2` lands behind item `1` in the sample

After the 12 m pipe (item 1) takes the strip along `y=0`, the remaining free rects inside
the 12 × 2.5 m truck are:

```
A = (x=0,    y=0.2,  l=12.0, w=2.3)   # the long strip beside the pipe
B = (x=12.0, y=0,    l=0.0,  w=2.5)   # zero after the pipe, truck is 12 m long
```

(B has zero length in this case because the truck is exactly 12 m.)

Item `2` is a 5.25 × 0.80 × 0.40 m pallet. Inside rect A, two rotations are tried:

- `5.25 × 0.80`: fits at `(0, 0.2)` → score `(frag, 0, 0.2, 5.25, 12*2.3 − 5.25*0.8, -0.8)`
- `0.80 × 5.25`: fits at `(0, 0.2)` → score `(frag, 0, 0.2, 0.8, …, -5.25)`

Items 3, 4, and 5 still to come are all long‑flat. The `5.25 × 0.80` rotation leaves a
cleaner 3.3‑m‑wide strip along the pipe for items 3 and 4 — its fragmentation penalty is
*lower* than rotating the pallet across the truck — so that rotation wins.

Then item `3` (3.30 × 0.80) places at `(5.25, 0.2)`, item `4` at `(5.25 + 3.30, 0.2)` =
`(8.55, 0.2)`, item `5` at `(8.55 + 3.30, 0.2)` = `(11.85, 0.2)`.

All five items land on the floor in a single line beside the pipe. No stacking was
needed — the floor heuristic alone compacts them, and `compact_truck_load` at the end
shifts any group leftward that can still move.

### 5.2 Stack score (`stack_score`)

```python
return (
    max(truck.used_length, placement.x2),  # 1. don't extend the truck just to stack
    -len(bases),                           # 2. prefer a stack sitting on more bases
    -support - connected,                  # 3. prefer higher, more contiguous support
    placement.x,                           # 4. back of truck first
    placement.y,                           # 5. then side of truck
)
```

The first term makes stacking **free** as long as the item fits above the area the truck
already uses. If stacking would also extend the truck, the optimizer will reconsider the
floor instead.

---

## 6. The outer loop — 240 plans, keep the best 10

`pack_once` is deterministic for a given seed, with a 15% chance of swapping each
adjacent pair in the sorted item list. `generate_candidates` runs `pack_once` 240 times
(default) with seeds 7…246, deduplicates plans by `(score, truck signature, unplaced IDs)`, and returns the top 10 by `plan_score`:

```python
plan_score = (
    unplaced_count,          # 1. ABSOLUTELY MINIMIZE unplaced goods
    truck_count,             # 2. then use fewer trucks
    -volume_utilization,     # 3. then pack denser
    -used_volume,
    total_remaining,         # 4. more usable leftover space is worse-looking later
    ...
)
```

Unplaced goods dominate everything else. Two plans that both ship every item compete on
truck count next, then on volumetric utilization, then on tie‑breakers.

---

## 7. Post-passes

After each plan is built:

1. `optimize_upper_levels` – tries to re-stack every upper‑level item to a better spot
  (lower `x2`, lower `z`). It never moves floor items. Runs up to 24 passes.
2. `compact_truck_load` – binary‑searches a leftward shift for each **floor-rooted
  group** (a pallet and everything on top of it) as long as no overlap or support is
   broken. This is what makes the final bed look neatly packed to the back.

---

## 8. Running it

```bash
python load_optimizer.py --goods goods_sample.csv --trucks trucks_sample.csv \
    --top-k 10 --attempts 240 --viz none
```

Key flags:

- `--top-k` – how many plans to report (default 10)
- `--attempts` – how many randomized packings to try (default 240)
- `--input-source json|csv|auto` – read from `GOODS_PAYLOAD` / `TRUCKS_PAYLOAD` instead
of CSV
- `--json-output stdout|both` – also emit the structured JSON result
- `--viz none` – skip the 3D preview

Typical text output (abbreviated):

```
Plan 1: unplaced=0 trucks=1 volume_usage=7.84/75.00m3 (10.4%) ... total_weight=1440.0kg
  Truck 1 (VC8543): items=5 weight=1440.0kg used_length=11.85m
    - ID 1 (Rør)     pos=(0.00,0.00,0.00) size=(12.00x0.20x0.20) weight=600.0kg level=0
    - ID 2 (Pall)    pos=(0.00,0.20,0.00) size=(5.25x0.80x0.40) weight=300.0kg level=0
    - ID 3 (Pall)    pos=(5.25,0.20,0.00) size=(3.30x0.80x0.65) weight=260.0kg level=0
    - ID 4 (Pall)    pos=(8.55,0.20,0.00) size=(3.30x0.80x0.60) weight=220.0kg level=0
    - ID 5 (PAll …)  pos=(11.85,0.20,0.00) size=(1.20x0.80x0.35) weight= 60.0kg level=0
```

---

## 9. Quick reference: which function does what


| Concern                    | Function                                                                                                                                           |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Parse CSV / JSON input     | `read_goods`, `read_trucks`, `parse_*_from_json`                                                                                                   |
| Can this `(x, y, z)` hold? | `can_place` (calls `within_truck`, `overlap_3d`, `support_ratio`, `contiguous_support_ratio`, `base_supports_stack`, `base_supports_weight_limit`) |
| Try floor placement        | `try_place_on_floor`                                                                                                                               |
| Try stacking               | `try_stack_merged_coplanar`, `try_stack_single_base`                                                                                               |
| Penalize bad splits        | `reserve_fragmentation_penalty`                                                                                                                    |
| Pick a new truck           | `create_new_truck`                                                                                                                                 |
| One full packing pass      | `pack_once`                                                                                                                                        |
| Generate and rank plans    | `generate_candidates`, `plan_score`                                                                                                                |
| Post-optimize a truck      | `optimize_upper_levels`, `compact_truck_load`                                                                                                      |


