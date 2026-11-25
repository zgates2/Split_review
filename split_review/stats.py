"""Per-episode and aggregate statistics in the exact shape lerobot v3.0 stores.

We call lerobot's own ``get_feature_stats`` / ``aggregate_stats`` so the numbers
and array shapes match the official writer byte-for-byte in structure. If the
library is unavailable we fall back to an equivalent local implementation.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

VIDEO_DTYPES = {"video", "image"}
_QUANTILES = [0.01, 0.10, 0.50, 0.90, 0.99]


def _local_feature_stats(array: np.ndarray, axis, keepdims: bool) -> dict[str, np.ndarray]:
    if axis == (0, 2, 3):
        batch, channels = array.shape[0], array.shape[1]
        flat = array.transpose(0, 2, 3, 1).reshape(-1, channels)
        count = batch
    else:
        flat = array if array.ndim > 1 else array.reshape(-1, 1)
        count = array.shape[0]
    stats = {
        "min": np.min(flat, axis=0),
        "max": np.max(flat, axis=0),
        "mean": np.mean(flat, axis=0),
        "std": np.std(flat, axis=0),
        "count": np.array([count]),
    }
    for q in _QUANTILES:
        stats[f"q{int(q * 100):02d}"] = np.quantile(flat, q, axis=0)
    return stats


def _feature_stats(array: np.ndarray, axis, keepdims: bool) -> dict[str, np.ndarray]:
    try:
        from lerobot.datasets.compute_stats import get_feature_stats

        return get_feature_stats(array, axis=axis, keepdims=keepdims)
    except Exception:
        return _local_feature_stats(array, axis, keepdims)


def episode_stats(
    rows: pa.Table,
    features: dict,
    frames: dict[str, list[np.ndarray]] | None = None,
) -> dict[str, dict[str, np.ndarray]]:
    """Stats for one output episode (rows + decoded frames per video key)."""
    stats: dict[str, dict[str, np.ndarray]] = {}
    for key, spec in features.items():
        dtype = spec.get("dtype")
        if dtype in VIDEO_DTYPES:
            imgs = (frames or {}).get(key)
            if not imgs:
                continue
            arr = np.stack(imgs).astype(np.float32)
            if arr.max() > 1.5:
                arr = arr / 255.0
            arr = arr.transpose(0, 3, 1, 2)  # N, C, H, W
            values = _feature_stats(arr, axis=(0, 2, 3), keepdims=True)
            # lerobot stores per-channel stats as (C,1,1); drop the batch axis
            stats[key] = {
                name: (v.reshape(v.shape[1:]) if v.ndim == 4 and v.shape[0] == 1 else v)
                for name, v in values.items()
            }
            continue
        if key not in rows.column_names:
            continue
        values = rows.column(key).to_pylist()
        if not values:
            continue
        arr = np.asarray(values, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        stats[key] = _feature_stats(arr, axis=0, keepdims=False)
    return stats


def aggregate(stats_list: list[dict[str, dict[str, np.ndarray]]]) -> dict[str, dict[str, np.ndarray]]:
    if not stats_list:
        return {}
    try:
        from lerobot.datasets.compute_stats import aggregate_stats

        return aggregate_stats(stats_list)
    except Exception:
        return _local_aggregate(stats_list)


def _local_aggregate(stats_list):
    keys = stats_list[0].keys()
    out = {}
    for feature in keys:
        counts = np.array([s[feature]["count"][0] for s in stats_list], dtype=np.float64)
        total = counts.sum()
        mins = np.min([s[feature]["min"] for s in stats_list], axis=0)
        maxs = np.max([s[feature]["max"] for s in stats_list], axis=0)
        means = np.stack([s[feature]["mean"] for s in stats_list])
        wmean = np.tensordot(counts, means, axes=(0, 0)) / total
        stds = np.stack([s[feature]["std"] for s in stats_list])
        vars_ = stds**2 + means**2
        wvar = np.tensordot(counts, vars_, axes=(0, 0)) / total - wmean**2
        out[feature] = {
            "min": mins,
            "max": maxs,
            "mean": wmean,
            "std": np.sqrt(np.maximum(wvar, 0)),
            "count": np.array([total]),
        }
        for q in ("q01", "q10", "q50", "q90", "q99"):
            qs = np.stack([s[feature][q] for s in stats_list])
            out[feature][q] = np.tensordot(counts, qs, axes=(0, 0)) / total
    return out


def to_python(value):
    """numpy -> nested python lists (for parquet / json)."""
    if isinstance(value, np.ndarray):
        return [to_python(v) for v in value.tolist()] if value.ndim else to_python(value.item())
    if isinstance(value, (np.generic,)):
        return value.item()
    if isinstance(value, (list, tuple)):
        return [to_python(v) for v in value]
    return value
