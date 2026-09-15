"""A Teachable Machine audio export, built in memory.

The robot's real model is per-robot and gitignored, so the tests build one with
the exact architecture and manifest layout Teachable Machine produces -- random
weights, real shapes. That exercises the loader and every layer's wiring; what
it cannot exercise is whether the trained weights recognise "NEO", which is what
`neo-audio-check --listen` is for.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

SHAPES = [
    ("NewHeadDense/kernel", [2000, 2]),
    ("NewHeadDense/bias", [2]),
    ("conv2d_1/kernel", [2, 8, 1, 8]),
    ("conv2d_1/bias", [8]),
    ("conv2d_2/kernel", [2, 4, 8, 32]),
    ("conv2d_2/bias", [32]),
    ("conv2d_3/kernel", [2, 4, 32, 32]),
    ("conv2d_3/bias", [32]),
    ("conv2d_4/kernel", [2, 4, 32, 32]),
    ("conv2d_4/bias", [32]),
    ("dense_1/kernel", [704, 2000]),
    ("dense_1/bias", [2000]),
]


def make_export(
    path: Path,
    *,
    shapes: list[tuple[str, list[int]]] | None = None,
    truncate: bool = False,
    labels: tuple[str, ...] = ("Background Noise", "Class 2"),
    seed: int = 7,
) -> Path:
    """Write a TM-shaped `.zip` at `path` and return it."""
    shapes = SHAPES if shapes is None else shapes
    rng = np.random.default_rng(seed)
    blobs = []
    for _name, shape in shapes:
        fan_in = int(np.prod(shape[:-1])) if len(shape) > 1 else 1
        blobs.append((rng.standard_normal(shape) / np.sqrt(max(fan_in, 1))).astype("<f4").tobytes())
    weights = b"".join(blobs)
    if truncate:
        weights = weights[: len(weights) // 2]

    model = {
        "modelTopology": {"class_name": "Model", "config": {"name": "model1", "layers": []}},
        "weightsManifest": [
            {"paths": ["weights.bin"], "weights": [{"name": n, "shape": s, "dtype": "float32"} for n, s in shapes]}
        ],
    }
    metadata = {"tfjsSpeechCommandsVersion": "0.4.0", "modelName": "TMv2", "wordLabels": list(labels)}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("model.json", json.dumps(model))
        z.writestr("metadata.json", json.dumps(metadata))
        z.writestr("weights.bin", weights)
    return path


@pytest.fixture(scope="session")
def tm_zip(tmp_path_factory) -> Path:
    return make_export(tmp_path_factory.mktemp("tm") / "neo_wakeword.zip")
