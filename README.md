# Load Optimizer

`load_optimizer.py` takes a list of **goods** (boxes, pallets, pipes, etc.) and a list of
available **trucks**, and produces the top‑K loading plans: which items go on which truck,
at which exact `(x, y, z)` position, in which rotation, and at which stacking level.
Hazard segregation between dangerous goods is enforced through a pairwise ADR matrix
(`hazard.csv`).

It is a 3D bin‑packing heuristic built around four ideas:

1. **Heavy / long items first** – hardest to fit, so they drive truck choice.
2. **Floor first, stack second** – never stack until the floor is committed.
3. **Pick the smallest truck that still fits** – so we don’t waste a 15 m trailer on a 5 m load.
4. **ADR labels travel together only when the matrix says so** – every truck carries the
  cumulative set of hazard labels of everything already loaded; a new item is rejected if
  the resulting label set would violate any pairwise rule or any class‑mix rule.

---

## 1. Inputs

Three files (or equivalent JSON payloads). Samples in the repo:

### `goods_sample.csv`

```csv
id,truck_id,name,weight_kg,l,b,h,stackable,max_stack,adr,adr_class
1,VC8543,1:Rør,600.00,12.00,0.20,0.20,true,1,true,1
2,VC8543,4.2:Pall,300.00,5.25,0.80,0.40,true,1,true,4.2
3,VC8543,no Adr:Pall (SBR)1,260.00,3.30,0.80,0.65,true,1,false,
4,VC8543,4.2:Pall (SBR),220.00,3.30,0.80,0.60,true,1,true,4.2
5,VC8543,1:adr,60.00,1.20,0.80,0.35,true,1,true,1
```


| Field           | Meaning                                                                  |
| --------------- | ------------------------------------------------------------------------ |
| `l, b, h`       | Length × breadth × height in meters                                      |
| `weight_kg`     | Weight of the item                                                       |
| `stackable`     | Can anything be placed on top of it?                                     |
| `max_stack`     | How many levels high this item can support above itself                  |
| `adr`           | Dangerous goods flag                                                     |
| `adr_class`     | Primary hazard label (e.g. `1`, `4.2`, `5.2`, `8`) when `adr=true`       |
| `adr_class_2`   | Optional subsidiary hazard label (read from CSV/JSON if present)         |


An item with `adr=true` contributes its non‑empty primary and subsidiary labels to the
truck it goes on (`Item.adr_label_set`). Non‑ADR items contribute nothing.

### `trucks_sample.csv`

```csv
id,name,l,b,h,max_weight_kg,max_volume_m3
BV2727,Bil BY2727 10x2.5m,10,2.5,2.5,60000,62.5
EW2068,Bil EW2068 15x2.5m,15,2.5,2.5,20000,93.75
RX5307,Bil RX5307 18x3.5m,18,3.5,2.8,18000,176.4
...
```

Trucks define the internal bed dimensions (`l × b × h`) and a weight ceiling. There is no
per‑truck ADR field anymore — every truck is treated as ADR‑capable, and segregation is
enforced **between items already on the truck and the candidate item** via the matrix.

### `hazard.csv` — ADR segregation matrix

```csv
Hazardlabel1,Hazardlabel2,ADRValue
1,1,9
1.4,1,9
2.1,1,0
4.2,1,0
5.1,1,5
...
```

A value of `1` or `9` means the pair may share a truck. Anything else (`0`, `2`, `5`, …)
forbids the combination. Missing entries are treated as forbidden, matching the original
Delphi reference (`bNotAllowed = TRUE` initialisation). Headers are normalized and
whitespace‑tolerant. The path is configurable with `--hazard-matrix` and the parsed
matrix is cached per resolved path.

---

## 2. Hard constraints

Every placement must satisfy all of these, or it is rejected:

1. **Inside the truck:** `0 ≤ x, x+l ≤ truck.l` (same for `y, z`).
2. **No overlap:** the 3D box of the new item cannot intersect any already‑placed box
  (`overlap_3d`).
3. **Weight:** `truck.current_weight + item.weight ≤ truck.max_weight`.
4. **Rotations:** only 90° rotations on the floor plane. Height axis stays vertical —
  pallets are not tipped over. If `l == w` only one rotation is tried
  (`Item.rotations_xy`).
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
9. **ADR segregation:** with the candidate’s labels added to the truck’s cumulative
  `adr_labels`, the resulting set must satisfy both the class‑mix rule and the pairwise
   matrix lookup (`truck_load_accepts_item_adr` → `adr_load_allowed`). See §3.

Constraints 1–8 are enforced inside `can_place`; constraint 9 is checked by `pack_once`
*before* trying any geometric placement on a given truck (so we never waste work
exploring a truck the item is forbidden to share).

---

## 3. ADR segregation in detail

Two rules are combined; both must hold.

### 3.1 Pairwise rule (matrix lookup)

For every pair of labels currently on the truck plus the candidate (including the
diagonal — a label paired with itself), `adr_pair_allowed` looks up `(a, b)` and then
`(b, a)` in the matrix. The pair is allowed only if `ADRValue ∈ {1, 9}`.

```python
ADR_ALLOWED_VALUES = frozenset({1, 9})
```

### 3.2 Class‑mix rule

Mirrors `adr_class_mix_allowed`. Labels are bucketed:

- `c1`  – any label starting with `1` (explosives family: `1`, `1.4`, `1.5`, `1.6`)
- `c52` – exactly `5.2`
- `c42` – exactly `4.2`
- `cother` – everything else

Forbidden combinations on the same truck:

- `c1 ∧ c52 ∧ cother`
- `c1 ∧ c42 ∧ cother`

That is, class 1 may travel with peroxides (5.2) or self‑heating (4.2) **only if no
third, unrelated hazard is also on board**.

### 3.3 How it is enforced during packing

`TruckLoad.adr_labels: Set[str]` accumulates the hazard labels of every item placed on
that truck. When `pack_once` considers placing item `X` on existing truck `T`:

```python
if not truck.can_take_weight(item.weight): continue
if not truck_load_accepts_item_adr(truck, item): continue   # ADR gate
... try geometric placement ...
```

`truck_load_accepts_item_adr` builds `candidate = T.adr_labels ∪ X.labels` and runs
`adr_load_allowed` on it. Only after that gate is passed does the optimizer try
`place_item_by_rules` for the geometry.

When the item must go on a fresh truck (`create_new_truck`), the new truck starts with an
empty `adr_labels` set, so single‑item ADR loads always pass — the matrix only matters as
soon as the truck has more than one labeled item.

In the JSON output, each truck reports `adr_labels_loaded` (a sorted list of all
labels currently on that truck). Per‑item `adr_class` and `adr_class_2` are also echoed
in placements and unplaced lists.

---

## 4. How a truck is chosen

Two moments matter: **sorting the candidate trucks** and **opening a new truck**.

### 4.1 Initial sort (`read_trucks` / `parse_trucks_from_json`)

Trucks are sorted longest‑first, then by floor area × height, then by payload:

```python
trucks.sort(key=lambda t: (t.l, t.w * t.h, t.max_weight), reverse=True)
```

This is only the display/iteration order — it is **not** the choice. Longest‑first helps
because a 12 m pipe has no chance on a 7 m truck, so we look at long trucks first when
asking "is there any truck this item could ever fit on?".

### 4.2 Re‑sorting trucks already in the plan (`pack_once`)

Each iteration of the main item loop re‑orders the open trucks before trying them:

```python
truck_order.sort(key=lambda idx: (trucks[idx].used_length, trucks[idx].current_weight))
```

This means we try the **least‑loaded truck first** — both length‑wise and weight‑wise.
That bias keeps long trucks from being filled with little items just because they were
opened first, and it lets earlier trucks act as "almost done" while a fresher one
absorbs new arrivals.

### 4.3 Opening a new truck (`create_new_truck`)

When an item cannot fit into any already‑opened truck (geometry, weight, or ADR), the
optimizer opens a fresh one. It filters `available_types` to those that are physically
compatible:

- `item.weight ≤ truck.max_weight`
- `item.h ≤ truck.h`
- The item footprint fits in either rotation: `(l ≤ t.l and w ≤ t.w)` or `(w ≤ t.l and l ≤ t.w)`

ADR is **not** part of this filter — ADR is a relationship between items on a truck, not a
truck attribute. Among compatible trucks, the **smallest sufficient** one wins:

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

## 5. How a placement is chosen

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
`rebuild_top_free_rects` runs first to refresh each base’s usable top rectangles.

### Step 4 – Stacking on a single base

Last resort: `try_stack_single_base` puts the item fully on top of one base pallet.

---

## 6. The placement scoring function

Both floor and stack passes use a tie‑breaker tuple, picked with `min(...)`. Lower is
better.

### 6.1 Floor score (`try_place_on_floor`)

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

### 6.2 Stack score (`stack_score`)

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

## 7. The outer loop — 240 plans, keep the best 10

`pack_once` is deterministic for a given seed, with a 15% chance of swapping each
adjacent pair in the sorted item list and a per‑item rotation shuffle. `generate_candidates`
runs `pack_once` 240 times (default) with seeds `7…246`, deduplicates plans by
`(score, truck signature, unplaced IDs)`, and returns the top 10 by `plan_score`:

```python
plan_score = (
    unplaced_count,          # 1. ABSOLUTELY MINIMIZE unplaced goods
    truck_count,             # 2. then use fewer trucks
    -volume_utilization,     # 3. then pack denser
    -used_volume,
    total_remaining,         # 4. more usable leftover space is worse-looking later
    floor_remaining,
    air_remaining,
    -max_floor_area,         # 5. keep the largest contiguous slot big
    -max_air_area,
    sum_used_length,
    sum_total_weight,
)
```

Unplaced goods dominate everything else. Two plans that both ship every item compete on
truck count next, then on volumetric utilization, then on tie‑breakers (remaining usable
volume, then largest contiguous floor/air slot, etc.).

---

## 8. Post‑passes

After each plan is built, every truck is post‑processed:

1. `optimize_upper_levels` – tries to re‑stack every upper‑level item to a better spot
  (lower `x2`, lower `z`). It never moves floor items. Runs up to 24 passes.
2. `compact_truck_load` – binary‑searches a leftward shift for each **floor‑rooted
  group** (a pallet and everything on top of it) as long as no overlap or support is
   broken. This is what makes the final bed look neatly packed to the back.

Both post‑passes preserve the ADR set on the truck (no items are added or removed, only
moved), so segregation cannot be invalidated.

---

## 9. Step‑by‑step: what happens when you run it

For a single invocation of `python load_optimizer.py …`:

1. **Parse CLI args.** `ADR_MATRIX_PATH` is set from `--hazard-matrix`. If the file
  exists, the matrix is parsed and cached eagerly (so the first packing call doesn’t pay
   for I/O).
2. **Load goods and trucks.** Either from JSON (`GOODS_PAYLOAD` / `TRUCKS_PAYLOAD`,
  selected via `--input-source`) or from the CSV files. Trucks are sorted longest‑first.
3. **Generate candidates.** `generate_candidates(items, trucks, count=top_k, attempts=N)`
  runs `pack_once` for seeds `7..N+6`, computes `plan_score`, and dedups.
4. **Inside each `pack_once`:**
  - Heavy‑first sort, plus 15% adjacent swaps and per‑item rotation shuffle (RNG seeded
    by the iteration seed).
   - For each item:
     - Sort current open trucks by `(used_length, current_weight)`.
     - For each open truck, check weight, then ADR (`truck_load_accepts_item_adr`), then
      try `place_item_by_rules` (floor‑in‑frontier → floor‑extend → merged stack → single
       stack). On success: append the placement, update weight, `used_length`, ADR
       labels, and rebuild top free rectangles.
     - If no open truck accepts the item, call `create_new_truck`. If a compatible truck
      exists, place the item there; otherwise mark it `unplaced`.
   - When all items are processed, run `optimize_upper_levels` and `compact_truck_load`
    on every truck.
5. **Rank and trim.** Plans are sorted by `plan_score`, deduplicated, and the top
  `--top-k` are returned.
6. **Report.** Text summary (`print_plan_summary`) is printed unless suppressed; JSON
  payload (`plans_to_json`) is printed when `--json-output` is `stdout` or `both`.
7. **Visualize.** Unless `--viz none`, distinct top plans are previewed with Plotly (or
  Matplotlib). With `--top-k > 1` the optimizer auto‑opens up to that many *visually
   distinct* plans (deduped via `plan_preview_signature`); otherwise it shows the single
   plan selected by `--plan-index`.

---

## 10. Running it

```bash
python load_optimizer.py \
    --goods goods_sample.csv \
    --trucks trucks_sample.csv \
    --hazard-matrix hazard.csv \
    --top-k 10 --attempts 240 --viz none
```

Key flags:

- `--goods`, `--trucks` – CSV paths (defaults: `goods_sample.csv`, `trucks_sample.csv`)
- `--hazard-matrix` – ADR matrix CSV path (default: `hazard.csv`). Required only when
  some items have `adr=true`; missing matrix file means every ADR pair is rejected.
- `--top-k` – how many plans to report (default 10)
- `--attempts` – how many randomized packings to try (default 240)
- `--input-source json|csv|auto` – read from `GOODS_PAYLOAD` / `TRUCKS_PAYLOAD` instead
  of CSV. `auto` uses JSON when both payloads are non‑empty, otherwise falls back to CSV.
- `--json-output none|stdout|both` – also emit the structured JSON result
- `--viz plotly|matplotlib|none` – 3D preview backend (default: plotly)
- `--plan-index N` – which plan to visualize when `--top-k 1`
- `--preview-best-two` – legacy flag forcing visualization of plans 1 and 2

Typical text output (abbreviated, with sample ADR mix from `goods_sample.csv`):

```
Plan 1: unplaced=0 trucks=1 volume_usage=7.84/75.00m3 (10.4%) ... total_weight=1440.0kg
  Truck 1 (VC8543): items=5 weight=1440.0kg used_length=11.85m
    - ID 1 (1:Rør)              pos=(0.00,0.00,0.00) size=(12.00x0.20x0.20) weight=600.0kg level=0
    - ID 2 (4.2:Pall)           pos=(0.00,0.20,0.00) size=(5.25x0.80x0.40) weight=300.0kg level=0
    - ID 3 (no Adr:Pall (SBR)1) pos=(5.25,0.20,0.00) size=(3.30x0.80x0.65) weight=260.0kg level=0
    - ID 4 (4.2:Pall (SBR))     pos=(8.55,0.20,0.00) size=(3.30x0.80x0.60) weight=220.0kg level=0
    - ID 5 (1:adr)              pos=(11.85,0.20,0.00) size=(1.20x0.80x0.35) weight= 60.0kg level=0
```

The same plan, in JSON, also reports `adr_labels_loaded` per truck (e.g. `["1", "4.2"]`)
and per‑placement `adr_class` / `adr_class_2`.

---

## 11. 3D preview

`preview_3d.py` renders each truck as a wireframe with colored mesh boxes for placed
items. ADR items show a hover line `ADR yes (class X)`. The Plotly subplot height is
`1500 px × number_of_trucks`, and the camera zoom is `0.10` — both tuned to keep multi‑
truck plans readable without manual rotation. Pass `use_matplotlib=True` (or `--viz
matplotlib`) for a static PNG‑style preview.

---

## 12. Quick reference: which function does what


| Concern                       | Function                                                                                                                                           |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Parse CSV / JSON input        | `read_goods`, `read_trucks`, `parse_items_from_json`, `parse_trucks_from_json`                                                                     |
| Load ADR matrix               | `load_adr_matrix` (cached per resolved path)                                                                                                       |
| Pairwise ADR rule             | `adr_pair_allowed`                                                                                                                                 |
| Class‑mix ADR rule            | `adr_class_mix_allowed` (uses `_classify_labels`)                                                                                                  |
| Whole‑truck ADR check         | `adr_load_allowed`, `truck_load_accepts_item_adr`                                                                                                  |
| Can this `(x, y, z)` hold?    | `can_place` (calls `within_truck`, `overlap_3d`, `support_ratio`, `contiguous_support_ratio`, `base_supports_stack`, `base_supports_weight_limit`) |
| Try floor placement           | `try_place_on_floor`                                                                                                                               |
| Try stacking                  | `try_stack_merged_coplanar`, `try_stack_single_base`                                                                                               |
| Penalize bad splits           | `reserve_fragmentation_penalty`                                                                                                                    |
| Pick a new truck              | `create_new_truck`                                                                                                                                 |
| One full packing pass         | `pack_once`                                                                                                                                        |
| Generate and rank plans       | `generate_candidates`, `plan_score`                                                                                                                |
| Post‑optimize a truck         | `optimize_upper_levels`, `compact_truck_load`                                                                                                      |
| Convert a plan to JSON / dict | `plans_to_json`, `plan_to_preview_dicts`, `plan_preview_signature`                                                                                 |
| 3D rendering                  | `visualize_plotly`, `visualize_matplotlib` (in `preview_3d.py`)                                                                                    |

