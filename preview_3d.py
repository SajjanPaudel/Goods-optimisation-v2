#!/usr/bin/env python3
from __future__ import annotations

import html
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


_MESH_I = [7, 0, 0, 0, 4, 4, 6, 6, 4, 0, 3, 2]
_MESH_J = [3, 4, 1, 2, 5, 6, 5, 2, 0, 1, 6, 3]
_MESH_K = [0, 7, 2, 3, 6, 7, 1, 1, 5, 5, 7, 6]
_UX = [0, 1, 1, 0, 0, 1, 1, 0]
_UY = [0, 0, 1, 1, 0, 0, 1, 1]
_UZ = [0, 0, 0, 0, 1, 1, 1, 1]

_COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#fffac8", "#800000", "#aaffc3", "#808000", "#ffd8b1",
    "#000075", "#808080", "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#17becf", "#7f7f7f", "#aec7e8", "#ffbb78",
]


def _mesh3d_box_vertices(x0: float, y0: float, z0: float, l: float, b: float, h: float) -> Tuple[List[float], List[float], List[float]]:
    x = [x0 + ux * l for ux in _UX]
    y = [y0 + uy * b for uy in _UY]
    z = [z0 + uz * h for uz in _UZ]
    return x, y, z


def _placement_hover_lines(p: Dict[str, Any]) -> str:
    name = html.escape(str(p["name"]))
    truck_id = html.escape(str(p["truck_id"]))
    adr_line = ""
    if p.get("adr", False):
        adr_class = str(p.get("adr_class", "") or "").strip()
        adr_line = f"<br><b>ADR</b> yes{f' (class {html.escape(adr_class)})' if adr_class else ''}"
    return (
        f"<b>ID {p['ID']}</b><br>{truck_id}{name}<br>"
        f"{adr_line}"
        f"<br><b>Dimensions</b> {p['l']:.3f} × {p['b']:.3f} × {p['h']:.3f} m<br>"
        f"<b>Position</b> {p['x']:.3f}, {p['y']:.3f}, {p['z']:.3f} m<br>"
        f"<b>Weight</b> {p['weight']:.1f} kg<br>"
        f"<b>Level</b> {p['level']}"
        "<extra></extra>"
    )


def _plotly_truck_wireframe_trace(l: float, b: float, h: float) -> Any:
    import plotly.graph_objects as go

    c = np.array(
        [
            [0, 0, 0],
            [l, 0, 0],
            [l, b, 0],
            [0, b, 0],
            [0, 0, h],
            [l, 0, h],
            [l, b, h],
            [0, b, h],
        ],
        dtype=float,
    )
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    xs: List[Optional[float]] = []
    ys: List[Optional[float]] = []
    zs: List[Optional[float]] = []
    for i, j in edges:
        xs.extend([c[i][0], c[j][0], None])
        ys.extend([c[i][1], c[j][1], None])
        zs.extend([c[i][2], c[j][2], None])
    return go.Scatter3d(
        x=xs,
        y=ys,
        z=zs,
        mode="lines",
        line=dict(color="rgba(40,50,70,0.95)", width=4),
        hoverinfo="skip",
        showlegend=False,
    )


def _plotly_weight_balance_traces(
    l: float,
    b: float,
    h: float,
    front_kg: float,
    back_kg: float,
) -> List[Any]:
    # Draw a bar above the truck (z > h) so it is readable regardless of cargo below.
    import plotly.graph_objects as go

    bar_z = h + max(0.35, h * 0.18)
    bar_y = b / 2.0
    mid = l / 2.0
    tick = max(0.12, h * 0.06)

    front_trace = go.Scatter3d(
        x=[0.0, mid],
        y=[bar_y, bar_y],
        z=[bar_z, bar_z],
        mode="lines",
        line=dict(color="rgba(40, 120, 220, 0.95)", width=12),
        hoverinfo="skip",
        showlegend=False,
        name="Front half",
    )
    back_trace = go.Scatter3d(
        x=[mid, l],
        y=[bar_y, bar_y],
        z=[bar_z, bar_z],
        mode="lines",
        line=dict(color="rgba(220, 90, 60, 0.95)", width=12),
        hoverinfo="skip",
        showlegend=False,
        name="Back half",
    )
    ticks = go.Scatter3d(
        x=[0.0, 0.0, None, mid, mid, None, l, l],
        y=[bar_y, bar_y, None, bar_y, bar_y, None, bar_y, bar_y],
        z=[bar_z - tick, bar_z + tick, None, bar_z - tick, bar_z + tick, None, bar_z - tick, bar_z + tick],
        mode="lines",
        line=dict(color="rgba(40,50,70,0.9)", width=4),
        hoverinfo="skip",
        showlegend=False,
    )
    labels = go.Scatter3d(
        x=[mid / 2.0, (mid + l) / 2.0],
        y=[bar_y, bar_y],
        z=[bar_z + tick * 3.0, bar_z + tick * 3.0],
        mode="text",
        text=[
            f"<b>Front {front_kg:.0f} kg</b>",
            f"<b>Back {back_kg:.0f} kg</b>",
        ],
        textfont=dict(size=16, color="rgb(30,40,60)"),
        textposition="middle center",
        hoverinfo="skip",
        showlegend=False,
    )
    return [front_trace, back_trace, ticks, labels]


def _plotly_truck_base_trace(l: float, b: float, truck_name: str) -> Any:
    import plotly.graph_objects as go

    safe_name = html.escape(str(truck_name))
    hover = (
        f"<b>Truck base</b><br>{safe_name}<br>"
        f"<b>l × b</b> {l:.3f} × {b:.3f} m"
        "<extra></extra>"
    )
    return go.Surface(
        x=[[0.0, l], [0.0, l]],
        y=[[0.0, 0.0], [b, b]],
        z=[[0.0, 0.0], [0.0, 0.0]],
        surfacecolor=[[1, 1], [1, 1]],
        colorscale=[[0.0, "rgb(225, 231, 242)"], [1.0, "rgb(225, 231, 242)"]],
        cmin=0,
        cmax=1,
        showscale=False,
        opacity=0.35,
        hovertemplate=hover,
        name="Truck base",
        showlegend=False,
        lighting=dict(ambient=1.0, diffuse=0.0, specular=0.0),
        contours=dict(x=dict(show=False), y=dict(show=False), z=dict(show=False)),
    )


def build_plotly_figure(plans: List[Dict[str, Any]], title: str) -> Any:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    n = len(plans)
    cols = min(2, max(n, 1))
    rows = (n + cols - 1) // cols
    titles: List[str] = []
    for p in plans:
        d = p["truck_dims"]
        tl, tb, th = d["l"], d["b"], d["h"]
        truck_volume = float(tl) * float(tb) * float(th)
        placed_volume = sum(float(item["l"]) * float(item["b"]) * float(item["h"]) for item in p["placements"])
        volume_util_pct = (placed_volume / truck_volume * 100.0) if truck_volume > 0 else 0.0
        titles.append(
            f"{p['truck']} {tl:g}m × {tb:g}m × {th:g}m<br>"
            f"{p['placed_count']} goods, {p['weight_util_pct']:.0f}% wt, "
            f"{placed_volume:.1f}/{truck_volume:.1f} m³ ({volume_util_pct:.0f}% vol)"
        )
    while len(titles) < rows * cols:
        titles.append("")

    fig = make_subplots(
        rows=rows,
        cols=cols,
        specs=[[{"type": "scatter3d"} for _ in range(cols)] for _ in range(rows)],
        subplot_titles=tuple(titles),
        vertical_spacing=0.01,
        horizontal_spacing=0.04,
    )
    fig.update_annotations(font=dict(size=18), yshift=-5)

    for idx, plan in enumerate(plans):
        r = idx // cols + 1
        c = idx % cols + 1
        dims = plan["truck_dims"]
        tl, tb, th = dims["l"], dims["b"], dims["h"]
        fig.add_trace(_plotly_truck_base_trace(tl, tb, plan["truck"]), row=r, col=c)
        fig.add_trace(_plotly_truck_wireframe_trace(tl, tb, th), row=r, col=c)

        front_kg = float(plan.get("front_weight_kg", 0.0))
        back_kg = float(plan.get("back_weight_kg", 0.0))
        for trace in _plotly_weight_balance_traces(tl, tb, th, front_kg, back_kg):
            fig.add_trace(trace, row=r, col=c)

        for i, p in enumerate(plan["placements"]):
            vx, vy, vz = _mesh3d_box_vertices(p["x"], p["y"], p["z"], p["l"], p["b"], p["h"])
            color = _COLORS[i % len(_COLORS)]
            fig.add_trace(
                go.Mesh3d(
                    x=vx,
                    y=vy,
                    z=vz,
                    i=_MESH_I,
                    j=_MESH_J,
                    k=_MESH_K,
                    color=color,
                    opacity=1.0,
                    hovertemplate=_placement_hover_lines(p),
                    name=f"ID {p['ID']}",
                    showlegend=False,
                    lighting=dict(ambient=0.55, diffuse=0.8, specular=0.0),
                    flatshading=True,
                ),
                row=r,
                col=c,
            )

    fig.update_layout(
        title=dict(text=title, x=0.5, xanchor="center", y=0.985, yanchor="top"),
        margin=dict(l=0, r=0, t=100, b=0),
        height=1000 * rows,
    )

    zoom = 0.12
    scene_layout: Dict[str, Any] = {}
    for idx, plan in enumerate(plans):
        dims = plan["truck_dims"]
        tl, tb, th = dims["l"], dims["b"], dims["h"]
        sid = "scene" if idx == 0 else f"scene{idx + 1}"
        z_pad = max(0.9, th * 0.55)
        scene_layout[sid] = dict(
            xaxis=dict(title="Length (m)", range=[0, tl], backgroundcolor="rgb(248,248,248)"),
            yaxis=dict(title="Width (m)", range=[0, tb], backgroundcolor="rgb(248,248,248)"),
            zaxis=dict(title="Height (m)", range=[0, th + z_pad], backgroundcolor="rgb(248,248,248)"),
            aspectmode="manual",
            aspectratio=dict(x=float(tl), y=float(tb), z=float(th) + float(z_pad)),
            camera=dict(eye=dict(x=1.35 / zoom, y=-1.55 / zoom, z=0.85 / zoom)),
        )
    fig.update_layout(**scene_layout)


    fig.update_layout(
            title_text=title,
            paper_bgcolor="gray",  # Color of the whole canvas
        )

    fig.update_scenes(
            xaxis_backgroundcolor="rgba(230, 230, 230, 0.5)",
            yaxis_backgroundcolor="rgba(230, 230, 230, 0.5)",
            zaxis_backgroundcolor="rgba(230, 230, 230, 0.5)",
            bgcolor="white", # Color inside the 3D axes
        )
    return fig


def _box_faces(
    x: float, y: float, z: float, l: float, b: float, h: float
) -> List[List[Tuple[float, float, float]]]:
    pts = np.array(
        [
            [x, y, z],
            [x + l, y, z],
            [x + l, y + b, z],
            [x, y + b, z],
            [x, y, z + h],
            [x + l, y, z + h],
            [x + l, y + b, z + h],
            [x, y + b, z + h],
        ]
    )
    return [
        [pts[j] for j in [0, 1, 2, 3]],
        [pts[j] for j in [4, 5, 6, 7]],
        [pts[j] for j in [0, 1, 5, 4]],
        [pts[j] for j in [2, 3, 7, 6]],
        [pts[j] for j in [1, 2, 6, 5]],
        [pts[j] for j in [0, 3, 7, 4]],
    ]


def _base_face(l: float, b: float) -> List[List[Tuple[float, float, float]]]:
    return [[(0.0, 0.0, 0.0), (l, 0.0, 0.0), (l, b, 0.0), (0.0, b, 0.0)]]


def show_load_preview(
    plans: List[Dict[str, Any]],
    *,
    save_path: Optional[str] = None,
    dpi: int = 120,
    use_matplotlib: bool = False,
    title: str = "Load preview",
) -> None:
    if save_path and save_path.lower().endswith(".html"):
        fig = build_plotly_figure(plans, title)
        fig.write_html(save_path, include_plotlyjs="cdn")
        return

    if not use_matplotlib:
        try:
            fig = build_plotly_figure(plans, title)
            fig.show()
            return
        except ImportError:
            pass

    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    n = len(plans)
    cols = min(2, max(n, 1))
    rows = (n + cols - 1) // cols
    fig = plt.figure(figsize=(6 * cols, 5 * rows))
    for i, plan in enumerate(plans):
        ax = fig.add_subplot(rows, cols, i + 1, projection="3d")
        dims = plan["truck_dims"]
        tl, tb, th = dims["l"], dims["b"], dims["h"]
        hull = Poly3DCollection(
            _base_face(tl, tb),
            facecolors=(0.8, 0.84, 0.9, 0.35),
            edgecolors=(0.35, 0.4, 0.5, 0.95),
            linewidths=1.0,
        )
        ax.add_collection3d(hull)
        for j, p in enumerate(plan["placements"]):
            poly = Poly3DCollection(
                _box_faces(p["x"], p["y"], p["z"], p["l"], p["b"], p["h"]),
                facecolors=_COLORS[j % len(_COLORS)],
                edgecolors="k",
                linewidths=0.6,
                alpha=1.0,
            )
            ax.add_collection3d(poly)
        z_pad = max(0.9, th * 0.55)
        ax.set_xlim(0, tl)
        ax.set_ylim(0, tb)
        ax.set_zlim(0, th + z_pad)
        try:
            ax.set_box_aspect((float(tl), float(tb), float(th)))
        except AttributeError:
            pass
        ax.set_xlabel("Length (m)")
        ax.set_ylabel("Width (m)")
        ax.set_zlabel("Height (m)")
        front_kg = float(plan.get("front_weight_kg", 0.0))
        back_kg = float(plan.get("back_weight_kg", 0.0))
        ax.set_title(f"{plan['truck']}  |  Front {front_kg:.0f} kg  /  Back {back_kg:.0f} kg")
        mid_x = tl / 2.0
        bar_y = tb / 2.0
        bar_z = th + max(0.35, th * 0.18)
        ax.plot([0, mid_x], [bar_y, bar_y], [bar_z, bar_z], color="#2878dc", linewidth=3)
        ax.plot([mid_x, tl], [bar_y, bar_y], [bar_z, bar_z], color="#dc5a3c", linewidth=3)
        ax.text(mid_x / 2.0, bar_y, bar_z + 0.15, f"Front {front_kg:.0f} kg", color="#2878dc", ha="center")
        ax.text((mid_x + tl) / 2.0, bar_y, bar_z + 0.15, f"Back {back_kg:.0f} kg", color="#dc5a3c", ha="center")
        ax.view_init(elev=22, azim=-60)
    fig.suptitle(title)
    fig.tight_layout()
    if save_path:
        fig.savefig(save_path, dpi=dpi, bbox_inches="tight")
        plt.close(fig)
    else:
        plt.show()
