#!/usr/bin/env python3
"""Prepare QuiltNet artifacts for bounded, memory-mapped CPU retrieval.

The generated files are immutable serving artifacts.  The JSON fragment can
be copied into a research manifest record after the two .npy files are
published to the configured artifact store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np


def _download_if_needed(source: str, temporary_dir: Path) -> Path:
    parsed = urlparse(source)
    if parsed.scheme in {"", "file"}:
        return Path(parsed.path if parsed.scheme else source).expanduser()
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError("source must be a local path or s3 URI")
    import boto3

    target = temporary_dir / Path(parsed.path).name
    client = boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None,
    )
    client.download_file(parsed.netloc, parsed.path.lstrip("/"), str(target))
    return target


def _load_array(source: Path, dataset: str) -> np.ndarray:
    suffix = source.suffix.lower()
    if suffix == ".npy":
        return np.load(source, allow_pickle=False)
    if suffix in {".h5", ".hdf5"}:
        import h5py

        with h5py.File(source, "r") as handle:
            if dataset not in handle:
                raise ValueError(f"{source.name} has no {dataset} dataset")
            return handle[dataset][:]
    if suffix in {".pt", ".pth"}:
        import torch

        value: Any = torch.load(source, map_location="cpu", weights_only=True)
        if isinstance(value, dict):
            aliases = (dataset, "embeddings") if dataset == "features" else (dataset,)
            for alias in aliases:
                if alias in value:
                    value = value[alias]
                    break
        if hasattr(value, "detach"):
            value = value.detach().cpu().numpy()
        return np.asarray(value)
    raise ValueError(f"unsupported artifact format: {source.suffix}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_uri(path: Path, prefix: str) -> str:
    if prefix:
        return f"{prefix.rstrip('/')}/{path.name}"
    return str(path.resolve())


def prepare(
    features_source: str,
    coordinates_source: str,
    output_dir: Path,
    artifact_prefix: str = "",
    force: bool = False,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    features_path = output_dir / "features.normalized.npy"
    coordinates_path = output_dir / "coordinates.npy"
    metadata_path = output_dir / "serving_artifact.json"
    targets = (features_path, coordinates_path, metadata_path)
    if not force and any(path.exists() for path in targets):
        raise FileExistsError("output exists; choose a new directory or pass --force")

    with tempfile.TemporaryDirectory(prefix="quiltnet-prepare-") as temporary:
        temporary_dir = Path(temporary)
        features = _load_array(
            _download_if_needed(features_source, temporary_dir), "features"
        )
        coordinates = _load_array(
            _download_if_needed(coordinates_source, temporary_dir), "coords"
        )

    if features.ndim != 2 or features.shape[1] == 0:
        raise ValueError("features must be a non-empty two-dimensional matrix")
    if coordinates.ndim != 2 or coordinates.shape[1] < 2:
        raise ValueError("coordinates must be a two-dimensional array with x and y")
    if features.shape[0] != coordinates.shape[0]:
        raise ValueError("features and coordinates must have the same row count")
    if not np.isfinite(features).all() or not np.isfinite(coordinates[:, :2]).all():
        raise ValueError("features and coordinates must contain only finite values")

    values = np.ascontiguousarray(features, dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    normalized = np.zeros_like(values, dtype=np.float32, order="C")
    valid = norms[:, 0] > 0
    normalized[valid] = values[valid] / norms[valid]
    coords = np.ascontiguousarray(coordinates[:, :2], dtype=np.float32)
    np.save(features_path, normalized, allow_pickle=False)
    np.save(coordinates_path, coords, allow_pickle=False)

    record = {
        "version": 1,
        "features_uri": _artifact_uri(features_path, artifact_prefix),
        "coordinates_uri": _artifact_uri(coordinates_path, artifact_prefix),
        "features_sha256": _sha256(features_path),
        "coordinates_sha256": _sha256(coordinates_path),
        "shape": list(normalized.shape),
        "coordinates_shape": list(coords.shape),
        "dtype": "float32",
        "coordinates_dtype": "float32",
        "normalized": True,
        "source_features_uri": features_source,
        "source_coordinates_uri": coordinates_source,
    }
    metadata_path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True, help="legacy feature artifact path or s3 URI")
    parser.add_argument("--coordinates", required=True, help="coordinate artifact path or s3 URI")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--artifact-prefix",
        default="",
        help="published URI prefix used in the manifest fragment",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    record = prepare(
        args.features,
        args.coordinates,
        args.output_dir,
        artifact_prefix=args.artifact_prefix,
        force=args.force,
    )
    print(json.dumps({"serving_artifact": record}, indent=2))


if __name__ == "__main__":
    main()
