"""QuiltNet text-to-tile retrieval.

The pixel service can call this module in a dedicated worker.  Heavy ML
dependencies are imported lazily so the normal tile-server image remains
small and the existing published-region fallback remains available during a
rolling deployment.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3

from .config import settings

logger = logging.getLogger(__name__)


class QuiltNetUnavailable(RuntimeError):
    """Raised when the optional QuiltNet runtime or an artifact is unavailable."""


_model_lock = threading.Lock()
_model_bundle: tuple[Any, Any, str] | None = None
_artifact_lock = threading.Lock()


def _artifact_path(uri: str) -> Path:
    cache_root = Path(settings.research_embedding_cache_dir).expanduser()
    cache_root.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(uri).path).suffix or ".bin"
    return cache_root / f"{hashlib.sha256(uri.encode()).hexdigest()[:32]}{suffix}"


def _download_artifact(uri: str) -> Path:
    path = _artifact_path(uri)
    with _artifact_lock:
        if path.exists() and path.stat().st_size > 0:
            return path
        return _download_artifact_locked(uri, path)


def _download_artifact_locked(uri: str, path: Path) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme in {"", "file"}:
        source = Path(parsed.path if parsed.scheme else uri).expanduser()
        if not source.exists():
            raise QuiltNetUnavailable(f"QuiltNet artifact is unavailable: {source.name}")
        return source
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise QuiltNetUnavailable("QuiltNet artifact URI must be a local path or s3 URI")
    temporary = path.with_suffix(path.suffix + ".partial")
    try:
        client = boto3.client(
            "s3",
            region_name=settings.agent_region or None,
            endpoint_url=settings.aws_endpoint_url or None,
        )
        client.download_file(parsed.netloc, parsed.path.lstrip("/"), str(temporary))
        os.replace(temporary, path)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise QuiltNetUnavailable("Unable to download QuiltNet artifact") from exc
    return path


def _select_device(torch: Any) -> str:
    requested = settings.quiltnet_device.strip().lower()
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cpu":
        return "cpu"
    if requested.startswith("cuda"):
        if not torch.cuda.is_available():
            raise QuiltNetUnavailable(
                "QuiltNet CUDA is configured but no CUDA device is available"
            )
        return requested
    raise QuiltNetUnavailable(
        f"Unsupported QuiltNet device {settings.quiltnet_device!r}; use auto, cpu, or cuda"
    )


def runtime_info() -> dict[str, Any]:
    requested = settings.quiltnet_device.strip().lower()
    try:
        import torch
    except ImportError:
        return {
            "configured_device": requested,
            "active_device": "unavailable",
            "cuda_available": False,
            "model_loaded": _model_bundle is not None,
        }

    cuda_available = bool(torch.cuda.is_available())
    if _model_bundle is not None:
        active_device = _model_bundle[2]
    elif requested == "auto":
        active_device = "cuda" if cuda_available else "cpu"
    elif requested.startswith("cuda") and not cuda_available:
        active_device = "unavailable"
    else:
        active_device = requested
    return {
        "configured_device": requested,
        "active_device": active_device,
        "cuda_available": cuda_available,
        "model_loaded": _model_bundle is not None,
    }


def _load_model() -> tuple[Any, Any, str]:
    global _model_bundle
    if _model_bundle is not None:
        return _model_bundle
    with _model_lock:
        if _model_bundle is not None:
            return _model_bundle
        try:
            import open_clip
            import torch
        except ImportError as exc:
            raise QuiltNetUnavailable(
                "QuiltNet retrieval runtime is not installed"
            ) from exc
        try:
            model_name = settings.quiltnet_model_name
            device = _select_device(torch)
            model, _, _ = open_clip.create_model_and_transforms(
                model_name, device=device
            )
            model.eval()
            tokenizer = open_clip.get_tokenizer(model_name)
        except Exception as exc:
            raise QuiltNetUnavailable("Unable to load the QuiltNet text encoder") from exc
        _model_bundle = (model, tokenizer, device)
        logger.info(
            "Loaded QuiltNet text encoder %s on %s",
            settings.quiltnet_model_name,
            device,
        )
        return _model_bundle


def _as_numpy(value: Any) -> Any:
    try:
        return value.detach().cpu().numpy()
    except AttributeError:
        return value


def _load_features(uri: str) -> tuple[Any, dict[str, Any]]:
    path = _download_artifact(uri)
    suffix = path.suffix.lower()
    metadata: dict[str, Any] = {}
    try:
        if suffix in {".h5", ".hdf5"}:
            import h5py
            with h5py.File(path, "r") as handle:
                if "features" not in handle:
                    raise QuiltNetUnavailable("QuiltNet HDF5 artifact has no features dataset")
                features = handle["features"][:]
                metadata.update({key: value.tolist() if hasattr(value, "tolist") else value for key, value in handle.attrs.items()})
                if "coords" in handle:
                    metadata["embedded_coords"] = handle["coords"][:]
            return features, metadata
        import torch
        value = torch.load(path, map_location="cpu", weights_only=True)
        if isinstance(value, dict):
            value = value.get("features", value.get("embeddings", value))
        return _as_numpy(value), metadata
    except QuiltNetUnavailable:
        raise
    except Exception as exc:
        raise QuiltNetUnavailable("Unable to load QuiltNet feature artifact") from exc


def _load_coordinates(uri: str | None, embedded: Any = None) -> tuple[Any, dict[str, Any]]:
    if embedded is not None:
        return embedded, {}
    if not uri:
        raise QuiltNetUnavailable("QuiltNet slide has no coordinate artifact")
    path = _download_artifact(uri)
    suffix = path.suffix.lower()
    try:
        if suffix in {".h5", ".hdf5"}:
            import h5py
            with h5py.File(path, "r") as handle:
                if "coords" not in handle:
                    raise QuiltNetUnavailable("QuiltNet coordinate artifact has no coords dataset")
                coords = handle["coords"][:]
                metadata = {
                    key: value.tolist() if hasattr(value, "tolist") else value
                    for key, value in handle.attrs.items()
                }
            return coords, metadata
        import numpy as np
        return np.load(path), {}
    except QuiltNetUnavailable:
        raise
    except Exception as exc:
        raise QuiltNetUnavailable("Unable to load QuiltNet coordinate artifact") from exc


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _infer_patch_size(coords: Any, metadata: dict[str, Any], model_record: dict[str, Any]) -> float:
    explicit = _finite_float(
        model_record.get("patch_size")
        or model_record.get("patch_size_level0")
        or metadata.get("patch_size")
    )
    if explicit and explicit > 0:
        return explicit
    try:
        import numpy as np
        values = np.asarray(coords)
        diffs = []
        for axis in (0, 1):
            unique = np.unique(values[:, axis])
            positive = np.diff(unique)
            diffs.extend(float(item) for item in positive if item > 0)
        if diffs:
            return min(diffs)
    except Exception:
        pass
    return 224.0


def _prompt_variants(concept: str) -> list[str]:
    return [
        concept,
        f"an H&E histopathology image of {concept}",
        f"histopathology showing {concept}",
    ]


class QuiltNetRetriever:
    """Lazy retrieval over one slide's published tile embeddings."""

    def search(
        self,
        *,
        model_id: str,
        model_record: dict[str, Any],
        query_plan: dict[str, Any],
        top_k: int,
        slide_width: int,
        slide_height: int,
    ) -> list[dict[str, Any]]:
        if model_id != "quiltnet_pmb":
            raise QuiltNetUnavailable(f"QuiltNet is not available for model {model_id}")
        if slide_width <= 0 or slide_height <= 0:
            raise QuiltNetUnavailable("Slide dimensions are required for QuiltNet search")
        features_uri = model_record.get("features_uri")
        if not features_uri:
            raise QuiltNetUnavailable("QuiltNet slide has no feature artifact")

        features, feature_metadata = _load_features(str(features_uri))
        coordinates, coordinate_metadata = _load_coordinates(
            model_record.get("coordinates_uri"), feature_metadata.get("embedded_coords")
        )
        import numpy as np

        features = np.asarray(features, dtype=np.float32)
        coordinates = np.asarray(coordinates, dtype=np.float32)
        if features.ndim != 2 or coordinates.ndim != 2 or coordinates.shape[1] < 2:
            raise QuiltNetUnavailable("QuiltNet artifacts have invalid dimensions")
        count = min(features.shape[0], coordinates.shape[0])
        if count == 0:
            return []
        features = features[:count]
        coordinates = coordinates[:count, :2]
        norms = np.linalg.norm(features, axis=1, keepdims=True)
        valid = norms[:, 0] > 0
        normalized = np.zeros_like(features)
        normalized[valid] = features[valid] / norms[valid]

        model, tokenizer, device = _load_model()
        positive = [str(item).strip() for item in query_plan.get("positive", []) if str(item).strip()]
        negative = [str(item).strip() for item in query_plan.get("negative", []) if str(item).strip()]
        if not positive:
            positive = [str(query_plan.get("primary", "")).strip()]
        positive = positive[:4]
        negative = negative[:2]
        prompts = [prompt for concept in positive for prompt in _prompt_variants(concept)]
        negative_prompts = [prompt for concept in negative for prompt in _prompt_variants(concept)]
        import torch

        with torch.inference_mode():
            positive_tokens = tokenizer(prompts)
            if hasattr(positive_tokens, "to"):
                positive_tokens = positive_tokens.to(device)
            positive_vectors = _as_numpy(model.encode_text(positive_tokens)).astype(np.float32)
            positive_vectors /= np.maximum(np.linalg.norm(positive_vectors, axis=1, keepdims=True), 1e-8)
            query_vector = positive_vectors.mean(axis=0)
            query_vector /= max(float(np.linalg.norm(query_vector)), 1e-8)
            if device.startswith("cuda"):
                feature_tensor = torch.from_numpy(normalized).to(device)
                query_tensor = torch.from_numpy(query_vector).to(device)
                scores = _as_numpy(feature_tensor @ query_tensor).astype(np.float32)
            else:
                scores = normalized @ query_vector
            if negative_prompts:
                negative_tokens = tokenizer(negative_prompts)
                if hasattr(negative_tokens, "to"):
                    negative_tokens = negative_tokens.to(device)
                negative_vectors = _as_numpy(model.encode_text(negative_tokens)).astype(np.float32)
                negative_vectors /= np.maximum(np.linalg.norm(negative_vectors, axis=1, keepdims=True), 1e-8)
                negative_vector = negative_vectors.mean(axis=0)
                negative_vector /= max(float(np.linalg.norm(negative_vector)), 1e-8)
                if device.startswith("cuda"):
                    negative_tensor = torch.from_numpy(negative_vector).to(device)
                    scores -= 0.35 * _as_numpy(feature_tensor @ negative_tensor).astype(np.float32)
                else:
                    scores -= 0.35 * (normalized @ negative_vector)

        candidate_order = np.argsort(-scores)[: min(count, max(top_k * 12, top_k))]
        patch_size = _infer_patch_size(coordinates, coordinate_metadata, model_record)
        selected: list[int] = []
        # Avoid returning a wall of adjacent tiles from the same hotspot.
        min_distance = max(8.0, min(35.0, 2_000.0 / max(top_k, 1)))
        for index in candidate_order.tolist():
            x = float(coordinates[index, 0]) / slide_width * 1000.0
            y = float(coordinates[index, 1]) / slide_height * 1000.0
            if any(
                math.hypot(
                    x - float(coordinates[other, 0]) / slide_width * 1000.0,
                    y - float(coordinates[other, 1]) / slide_height * 1000.0,
                )
                < min_distance
                for other in selected
            ):
                continue
            selected.append(index)
            if len(selected) >= top_k:
                break

        best = float(scores[candidate_order[0]]) if len(candidate_order) else 0.0
        regions: list[dict[str, Any]] = []
        for rank, index in enumerate(selected, start=1):
            x = max(0.0, min(1000.0, float(coordinates[index, 0]) / slide_width * 1000.0))
            y = max(0.0, min(1000.0, float(coordinates[index, 1]) / slide_height * 1000.0))
            width = max(1.0, min(1000.0 - x, patch_size / slide_width * 1000.0))
            height = max(1.0, min(1000.0 - y, patch_size / slide_height * 1000.0))
            score = float(scores[index])
            candidate_id = hashlib.sha1(
                f"{model_id}:{coordinates[index, 0]}:{coordinates[index, 1]}".encode()
            ).hexdigest()[:16]
            regions.append(
                {
                    "candidate_id": candidate_id,
                    "model": model_id,
                    "points": [{"x": x, "y": y}, {"x": x + width, "y": y + height}],
                    "score": score,
                    "similarity": score,
                    "relative_score": max(0.0, min(1.0, score / best)) if best > 0 else 0.0,
                    "matched_concepts": positive,
                    "verification": "semantic",
                    "rank": rank,
                }
            )
        return regions


_retriever: QuiltNetRetriever | None = None


def get_retriever() -> QuiltNetRetriever:
    global _retriever
    if _retriever is None:
        _retriever = QuiltNetRetriever()
    return _retriever
