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
import time
from collections import OrderedDict
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
_artifact_locks_guard = threading.Lock()
_artifact_locks: dict[str, threading.Lock] = {}
_cache_lock = threading.Lock()
_validated_artifacts: set[tuple[str, str]] = set()
_text_cache_lock = threading.Lock()
_text_embedding_cache: OrderedDict[tuple[str, str, str, str], Any] = OrderedDict()
_text_cache_hits = 0
_text_cache_misses = 0
_metrics_lock = threading.Lock()
_search_count = 0
_last_search_metrics: dict[str, float] = {}
_PROMPT_TEMPLATE_VERSION = "v1"
_WARM_CONCEPTS = ("tumor", "normal tissue", "carcinoma")


def _artifact_path(uri: str) -> Path:
    cache_root = Path(settings.research_embedding_cache_dir).expanduser()
    cache_root.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(uri).path).suffix or ".bin"
    return cache_root / f"{hashlib.sha256(uri.encode()).hexdigest()[:32]}{suffix}"


def _download_artifact(uri: str) -> Path:
    path = _artifact_path(uri)
    with _artifact_locks_guard:
        lock = _artifact_locks.setdefault(uri, threading.Lock())
    with lock:
        if path.exists() and path.stat().st_size > 0:
            path.touch()
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
    with _cache_lock:
        _prune_embedding_cache({path})
    return path


def _prune_embedding_cache(protected: set[Path] | None = None) -> None:
    """Keep the artifact cache bounded while leaving the current request intact."""
    maximum = max(0, int(settings.research_embedding_cache_max_bytes))
    if maximum <= 0:
        return
    cache_root = Path(settings.research_embedding_cache_dir).expanduser()
    protected = protected or set()
    try:
        files = [
            path
            for path in cache_root.iterdir()
            if path.is_file() and not path.name.endswith(".partial")
        ]
    except OSError:
        return
    total = sum(path.stat().st_size for path in files)
    if total <= maximum:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if path in protected:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue
        if total <= maximum:
            break


def _is_cached_path(path: Path) -> bool:
    try:
        return path.parent.resolve() == Path(settings.research_embedding_cache_dir).expanduser().resolve()
    except OSError:
        return False


def _verify_checksum(path: Path, expected: str | None) -> None:
    if not expected:
        return
    expected = expected.lower().removeprefix("sha256:")
    cache_key = (str(path), expected)
    with _cache_lock:
        if cache_key in _validated_artifacts:
            return
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise QuiltNetUnavailable("Unable to read QuiltNet artifact") from exc
    if digest.hexdigest().lower() != expected:
        if _is_cached_path(path):
            path.unlink(missing_ok=True)
        raise QuiltNetUnavailable("QuiltNet artifact checksum mismatch")
    with _cache_lock:
        _validated_artifacts.add(cache_key)


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
            "prompt_cache_size": 0,
            "prompt_cache_hits": _text_cache_hits,
            "prompt_cache_misses": _text_cache_misses,
            "search_count": _search_count,
            "last_search": {},
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
    with _metrics_lock:
        metrics = dict(_last_search_metrics)
    info = {
        "configured_device": requested,
        "active_device": active_device,
        "cuda_available": cuda_available,
        "model_loaded": _model_bundle is not None,
        "prompt_cache_size": len(_text_embedding_cache),
        "prompt_cache_hits": _text_cache_hits,
        "prompt_cache_misses": _text_cache_misses,
        "search_count": _search_count,
        "last_search": metrics,
    }
    if hasattr(torch, "get_num_threads"):
        info["torch_threads"] = int(torch.get_num_threads())
    if hasattr(torch, "get_num_interop_threads"):
        info["torch_interop_threads"] = int(torch.get_num_interop_threads())
    return info


def _record_search_metrics(**values: float) -> None:
    global _search_count, _last_search_metrics
    with _metrics_lock:
        _search_count += 1
        _last_search_metrics = {key: round(value, 2) for key, value in values.items()}


def _configure_torch(torch: Any) -> None:
    try:
        torch.set_num_threads(max(1, int(settings.quiltnet_torch_threads)))
    except (AttributeError, RuntimeError) as exc:
        logger.debug("Unable to configure QuiltNet torch intra-op threads: %s", exc)
    try:
        torch.set_num_interop_threads(max(1, int(settings.quiltnet_torch_interop_threads)))
    except (AttributeError, RuntimeError) as exc:
        logger.debug("Unable to configure QuiltNet torch inter-op threads: %s", exc)


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
            _configure_torch(torch)
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


def _text_cache_key(prompt: str) -> tuple[str, str, str, str]:
    model_name = settings.quiltnet_model_name
    return (
        model_name,
        f"open_clip-tokenizer:{model_name}",
        _PROMPT_TEMPLATE_VERSION,
        prompt,
    )


def _as_numpy(value: Any) -> Any:
    try:
        return value.detach().cpu().numpy()
    except AttributeError:
        return value


def _encode_text_vectors(
    prompts: list[str], model: Any, tokenizer: Any, device: str
) -> Any:
    """Return normalized prompt vectors, batching uncached prompts together."""
    import numpy as np
    import torch

    global _text_cache_hits, _text_cache_misses
    if not prompts:
        return np.empty((0, 0), dtype=np.float32)
    unique_prompts = list(dict.fromkeys(prompts))
    vectors: dict[str, Any] = {}
    missing: list[str] = []
    with _text_cache_lock:
        for prompt in unique_prompts:
            key = _text_cache_key(prompt)
            cached = _text_embedding_cache.get(key)
            if cached is None:
                missing.append(prompt)
            else:
                _text_embedding_cache.move_to_end(key)
                vectors[prompt] = cached
                _text_cache_hits += 1
        _text_cache_misses += len(missing)

    if missing:
        with torch.inference_mode():
            tokens = tokenizer(missing)
            if hasattr(tokens, "to"):
                tokens = tokens.to(device)
            encoded = np.atleast_2d(_as_numpy(model.encode_text(tokens))).astype(
                np.float32, copy=False
            )
        if encoded.shape[0] != len(missing) or encoded.ndim != 2:
            raise QuiltNetUnavailable("QuiltNet text encoder returned invalid dimensions")
        norms = np.linalg.norm(encoded, axis=1, keepdims=True)
        if not np.isfinite(encoded).all() or not np.isfinite(norms).all():
            raise QuiltNetUnavailable("QuiltNet text encoder returned non-finite values")
        normalized = encoded / np.maximum(norms, 1e-8)
        with _text_cache_lock:
            for prompt, vector in zip(missing, normalized, strict=True):
                key = _text_cache_key(prompt)
                cached_vector = np.ascontiguousarray(vector, dtype=np.float32)
                _text_embedding_cache[key] = cached_vector
                _text_embedding_cache.move_to_end(key)
                vectors[prompt] = cached_vector
            while len(_text_embedding_cache) > max(1, settings.quiltnet_prompt_cache_size):
                _text_embedding_cache.popitem(last=False)

    return np.stack([vectors[prompt] for prompt in prompts]).astype(np.float32, copy=False)


def warm_model() -> None:
    model, tokenizer, device = _load_model()
    prompts = [prompt for concept in _WARM_CONCEPTS for prompt in _prompt_variants(concept)]
    _encode_text_vectors(prompts, model, tokenizer, device)
    logger.info("Warmed QuiltNet text encoder on %s", device)


def _load_features(
    uri: str,
    *,
    expected_sha256: str | None = None,
    normalized: bool = False,
) -> tuple[Any, dict[str, Any]]:
    path = _download_artifact(uri)
    _verify_checksum(path, expected_sha256)
    suffix = path.suffix.lower()
    metadata: dict[str, Any] = {}
    try:
        if suffix == ".npy":
            import numpy as np

            features = np.load(path, mmap_mode="r", allow_pickle=False)
            metadata.update({"prepared": normalized, "normalized": normalized})
            return features, metadata
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


def _load_coordinates(
    uri: str | None,
    embedded: Any = None,
    *,
    expected_sha256: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    if embedded is not None:
        return embedded, {}
    if not uri:
        raise QuiltNetUnavailable("QuiltNet slide has no coordinate artifact")
    path = _download_artifact(uri)
    _verify_checksum(path, expected_sha256)
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
        return np.load(path, mmap_mode="r", allow_pickle=False), {}
    except QuiltNetUnavailable:
        raise
    except Exception as exc:
        raise QuiltNetUnavailable("Unable to load QuiltNet coordinate artifact") from exc


def _validate_prepared_metadata(
    features: Any, coordinates: Any, prepared: dict[str, Any]
) -> None:
    import numpy as np

    if not prepared.get("normalized"):
        raise QuiltNetUnavailable("QuiltNet prepared features must be normalized")
    if not prepared.get("features_sha256") or not prepared.get("coordinates_sha256"):
        raise QuiltNetUnavailable("QuiltNet prepared artifacts require checksums")
    expected_shape = prepared.get("shape")
    if expected_shape is not None and list(features.shape) != list(expected_shape):
        raise QuiltNetUnavailable("QuiltNet prepared feature shape does not match metadata")
    expected_coordinates_shape = prepared.get("coordinates_shape")
    if expected_coordinates_shape is not None and list(coordinates.shape) != list(
        expected_coordinates_shape
    ):
        raise QuiltNetUnavailable(
            "QuiltNet prepared coordinate shape does not match metadata"
        )
    if features.dtype != np.dtype(prepared.get("dtype", "float32")):
        raise QuiltNetUnavailable("QuiltNet prepared feature dtype does not match metadata")
    if coordinates.dtype != np.dtype(prepared.get("coordinates_dtype", "float32")):
        raise QuiltNetUnavailable(
            "QuiltNet prepared coordinate dtype does not match metadata"
        )
    if not features.flags.c_contiguous or not coordinates.flags.c_contiguous:
        raise QuiltNetUnavailable("QuiltNet prepared artifact is not contiguous")


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


def _prepared_artifact(model_record: dict[str, Any]) -> dict[str, Any]:
    value = model_record.get("serving_artifact")
    return value if isinstance(value, dict) else {}


def _normalize_feature_chunk(features: Any) -> Any:
    import numpy as np

    values = np.asarray(features, dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    normalized = np.zeros_like(values, dtype=np.float32, order="C")
    valid = norms[:, 0] > 0
    normalized[valid] = values[valid] / norms[valid]
    return normalized


def _score_features(features: Any, query_vector: Any, *, normalized: bool, device: str) -> Any:
    """Score a matrix in bounded chunks so cold artifacts do not fill RAM/VRAM."""
    import numpy as np

    count = int(features.shape[0])
    scores = np.empty(count, dtype=np.float32)
    chunk_size = max(1, int(settings.quiltnet_score_chunk_size))
    if device.startswith("cuda"):
        import torch

        query_tensor = torch.from_numpy(np.ascontiguousarray(query_vector)).to(device)
        with torch.inference_mode():
            for start in range(0, count, chunk_size):
                end = min(count, start + chunk_size)
                chunk = features[start:end]
                if not normalized:
                    chunk = _normalize_feature_chunk(chunk)
                feature_tensor = torch.from_numpy(np.ascontiguousarray(chunk)).to(device)
                scores[start:end] = _as_numpy(feature_tensor @ query_tensor).astype(
                    np.float32, copy=False
                )
        return scores

    for start in range(0, count, chunk_size):
        end = min(count, start + chunk_size)
        chunk = features[start:end]
        if not normalized:
            chunk = _normalize_feature_chunk(chunk)
        scores[start:end] = chunk @ query_vector
    return scores


def _top_indices(scores: Any, top_k: int) -> Any:
    """Select the candidate budget with deterministic score/index ordering."""
    import numpy as np

    count = int(scores.shape[0])
    if count == 0:
        return np.empty(0, dtype=np.int64)
    budget = min(count, max(int(top_k) * 12, int(top_k)))
    indices = np.arange(count, dtype=np.int64)
    if budget < count:
        threshold = np.partition(scores, count - budget)[count - budget]
        better = indices[scores > threshold]
        equal = indices[scores == threshold]
        needed = max(0, budget - len(better))
        candidates = np.concatenate((better, equal[:needed]))
    else:
        candidates = indices
    order = np.lexsort((candidates, -scores[candidates]))
    return candidates[order]


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
        started_at = time.perf_counter()
        if model_id != "quiltnet_pmb":
            raise QuiltNetUnavailable(f"QuiltNet is not available for model {model_id}")
        if slide_width <= 0 or slide_height <= 0:
            raise QuiltNetUnavailable("Slide dimensions are required for QuiltNet search")
        prepared = _prepared_artifact(model_record)
        features_uri = prepared.get("features_uri") or model_record.get("features_uri")
        if not features_uri:
            raise QuiltNetUnavailable("QuiltNet slide has no feature artifact")

        artifact_started_at = time.perf_counter()
        features, feature_metadata = _load_features(
            str(features_uri),
            expected_sha256=prepared.get("features_sha256"),
            normalized=bool(prepared.get("normalized", False)),
        )
        coordinates, coordinate_metadata = _load_coordinates(
            prepared.get("coordinates_uri") or model_record.get("coordinates_uri"),
            feature_metadata.get("embedded_coords"),
            expected_sha256=prepared.get("coordinates_sha256"),
        )
        artifact_ms = (time.perf_counter() - artifact_started_at) * 1000
        import numpy as np

        if prepared:
            _validate_prepared_metadata(features, coordinates, prepared)
        else:
            features = np.asarray(features, dtype=np.float32)
            coordinates = np.asarray(coordinates, dtype=np.float32)
        if features.ndim != 2 or coordinates.ndim != 2 or coordinates.shape[1] < 2:
            raise QuiltNetUnavailable("QuiltNet artifacts have invalid dimensions")
        if not np.isfinite(features).all() or not np.isfinite(coordinates[:, :2]).all():
            raise QuiltNetUnavailable("QuiltNet artifacts contain non-finite values")
        if features.shape[0] != coordinates.shape[0]:
            raise QuiltNetUnavailable("QuiltNet feature and coordinate counts do not match")
        count = features.shape[0]
        if count == 0:
            _record_search_metrics(
                total_ms=(time.perf_counter() - started_at) * 1000,
                artifact_ms=artifact_ms,
                text_ms=0.0,
                scoring_ms=0.0,
                tile_count=0.0,
            )
            return []
        coordinates = coordinates[:count, :2]

        model, tokenizer, device = _load_model()
        positive = [str(item).strip() for item in query_plan.get("positive", []) if str(item).strip()]
        negative = [str(item).strip() for item in query_plan.get("negative", []) if str(item).strip()]
        if not positive:
            positive = [str(query_plan.get("primary", "")).strip()]
        positive = positive[:4]
        negative = negative[:2]
        prompts = [prompt for concept in positive for prompt in _prompt_variants(concept)]
        negative_prompts = [prompt for concept in negative for prompt in _prompt_variants(concept)]
        text_started_at = time.perf_counter()
        positive_vectors = _encode_text_vectors(prompts, model, tokenizer, device)
        query_vector = positive_vectors.mean(axis=0)
        query_vector /= max(float(np.linalg.norm(query_vector)), 1e-8)
        if negative_prompts:
            negative_vectors = _encode_text_vectors(
                negative_prompts, model, tokenizer, device
            )
            negative_vector = negative_vectors.mean(axis=0)
            negative_vector /= max(float(np.linalg.norm(negative_vector)), 1e-8)
            query_vector = query_vector - 0.35 * negative_vector
        text_ms = (time.perf_counter() - text_started_at) * 1000

        if not np.isfinite(query_vector).all():
            raise QuiltNetUnavailable("QuiltNet query vector contains non-finite values")
        scoring_started_at = time.perf_counter()
        scores = _score_features(
            features,
            np.ascontiguousarray(query_vector, dtype=np.float32),
            normalized=bool(prepared.get("normalized", False)),
            device=device,
        )
        scoring_ms = (time.perf_counter() - scoring_started_at) * 1000
        candidate_order = _top_indices(scores, top_k)
        _record_search_metrics(
            total_ms=(time.perf_counter() - started_at) * 1000,
            artifact_ms=artifact_ms,
            text_ms=text_ms,
            scoring_ms=scoring_ms,
            tile_count=float(count),
        )
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
