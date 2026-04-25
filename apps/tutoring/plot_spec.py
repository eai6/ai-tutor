"""Validation + normalization for the data_interpretation `plot_spec`
field on ExitTicketQuestion.answer_data.

The frontend (templates/tutoring/chat_tutor.html) renders these as
interactive Chart.js plots. Keep this module Python-only — the
JavaScript renderer is the source of truth for what's visually
supported.

Schema (LLM-friendly, render-agnostic):

    {
        "type": "bar" | "line" | "pie" | "doughnut" | "scatter",
        "title": str (required),
        "x_label": str (optional, ignored for pie/doughnut),
        "y_label": str (optional, ignored for pie/doughnut),
        "labels": [str] (required for non-scatter),
        "datasets": [
            {
                "label": str,
                "data": [number] (non-scatter; aligned with labels),
                "points": [[x, y]] (scatter only; data omitted),
                "color": str (optional CSS color),
            }, ...
        ],
        "source": str (optional citation),
    }
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

ALLOWED_TYPES = {"bar", "line", "pie", "doughnut", "scatter"}


def is_valid_plot_spec(spec: Any) -> bool:
    """Convenience boolean wrapper for validate_plot_spec."""
    err = validate_plot_spec(spec)
    return err is None


def validate_plot_spec(spec: Any) -> Optional[str]:
    """Return None when the spec is renderable, else an error message."""
    if not isinstance(spec, dict):
        return "plot_spec must be an object"
    plot_type = spec.get("type")
    if plot_type not in ALLOWED_TYPES:
        return f"plot_spec.type must be one of {sorted(ALLOWED_TYPES)}"
    if not isinstance(spec.get("title", ""), str):
        return "plot_spec.title must be a string"

    datasets = spec.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        return "plot_spec.datasets must be a non-empty list"

    is_scatter = plot_type == "scatter"
    labels = spec.get("labels")
    if not is_scatter:
        if not isinstance(labels, list) or not labels:
            return "plot_spec.labels must be a non-empty list for non-scatter charts"
        if any(not isinstance(l, (str, int, float)) for l in labels):
            return "plot_spec.labels entries must be strings or numbers"

    for i, ds in enumerate(datasets):
        if not isinstance(ds, dict):
            return f"plot_spec.datasets[{i}] must be an object"
        if not isinstance(ds.get("label", ""), str):
            return f"plot_spec.datasets[{i}].label must be a string"
        if is_scatter:
            pts = ds.get("points")
            if not isinstance(pts, list) or not pts:
                return f"plot_spec.datasets[{i}].points must be a non-empty list of [x,y] pairs"
            for j, p in enumerate(pts):
                if (
                    not isinstance(p, (list, tuple))
                    or len(p) != 2
                    or not all(isinstance(v, (int, float)) for v in p)
                ):
                    return f"plot_spec.datasets[{i}].points[{j}] must be [number, number]"
        else:
            data = ds.get("data")
            if not isinstance(data, list) or not data:
                return f"plot_spec.datasets[{i}].data must be a non-empty list of numbers"
            if any(not isinstance(v, (int, float)) for v in data):
                return f"plot_spec.datasets[{i}].data entries must be numbers"
            if len(data) != len(labels):
                return (
                    f"plot_spec.datasets[{i}].data has {len(data)} values "
                    f"but labels has {len(labels)} — they must match"
                )
    return None


def coerce_plot_spec(spec: Any) -> Tuple[Optional[Dict], Optional[str]]:
    """Best-effort coercion: cleans numeric strings, strips Nones, and
    runs validation. Returns (cleaned_spec, error_or_None).

    Useful when the LLM emits values like "200,000" instead of 200000
    or wraps numbers in strings.
    """
    if not isinstance(spec, dict):
        return None, "plot_spec must be an object"
    cleaned: Dict[str, Any] = {}
    cleaned["type"] = (spec.get("type") or "bar").strip().lower()
    cleaned["title"] = str(spec.get("title", "") or "").strip()
    if spec.get("x_label"):
        cleaned["x_label"] = str(spec["x_label"]).strip()
    if spec.get("y_label"):
        cleaned["y_label"] = str(spec["y_label"]).strip()
    if spec.get("source"):
        cleaned["source"] = str(spec["source"]).strip()

    raw_labels = spec.get("labels") or []
    if isinstance(raw_labels, list):
        cleaned["labels"] = [str(l) if not isinstance(l, (int, float)) else l for l in raw_labels]

    cleaned_ds: List[Dict[str, Any]] = []
    for ds in spec.get("datasets") or []:
        if not isinstance(ds, dict):
            continue
        out_ds: Dict[str, Any] = {"label": str(ds.get("label", "Series") or "Series")}
        if ds.get("color"):
            out_ds["color"] = str(ds["color"])
        if cleaned["type"] == "scatter":
            pts: List[List[float]] = []
            for p in ds.get("points") or []:
                try:
                    if isinstance(p, dict) and "x" in p and "y" in p:
                        pts.append([float(p["x"]), float(p["y"])])
                    elif isinstance(p, (list, tuple)) and len(p) == 2:
                        pts.append([float(p[0]), float(p[1])])
                except (TypeError, ValueError):
                    continue
            out_ds["points"] = pts
        else:
            data: List[float] = []
            for v in ds.get("data") or []:
                if isinstance(v, str):
                    v = v.replace(",", "").strip().rstrip("%$")
                try:
                    data.append(float(v))
                except (TypeError, ValueError):
                    continue
            out_ds["data"] = data
        cleaned_ds.append(out_ds)
    cleaned["datasets"] = cleaned_ds

    err = validate_plot_spec(cleaned)
    if err:
        return None, err
    return cleaned, None
