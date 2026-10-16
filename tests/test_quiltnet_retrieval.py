import contextlib
import hashlib
import os
from types import SimpleNamespace

import numpy as np
import pytest

from app import quiltnet_retrieval


def fake_torch(cuda_available: bool):
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
    )


def test_quiltnet_auto_uses_cuda_when_available(monkeypatch):
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_device", "auto")

    assert quiltnet_retrieval._select_device(fake_torch(True)) == "cuda"


def test_quiltnet_auto_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_device", "auto")

    assert quiltnet_retrieval._select_device(fake_torch(False)) == "cpu"


def test_quiltnet_explicit_cuda_requires_a_gpu(monkeypatch):
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_device", "cuda")

    with pytest.raises(
        quiltnet_retrieval.QuiltNetUnavailable,
        match="no CUDA device is available",
    ):
        quiltnet_retrieval._select_device(fake_torch(False))


def test_quiltnet_rejects_unknown_device(monkeypatch):
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_device", "tpu")

    with pytest.raises(
        quiltnet_retrieval.QuiltNetUnavailable,
        match="Unsupported QuiltNet device",
    ):
        quiltnet_retrieval._select_device(fake_torch(False))


def test_chunked_cpu_scores_match_full_normalized_baseline(monkeypatch):
    features = np.array(
        [[3.0, 4.0], [1.0, 0.0], [0.0, 0.0], [2.0, 2.0], [-1.0, 2.0]],
        dtype=np.float32,
    )
    query = np.array([0.6, 0.8], dtype=np.float32)
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_score_chunk_size", 2)

    actual = quiltnet_retrieval._score_features(
        features, query, normalized=False, device="cpu"
    )
    norms = np.linalg.norm(features, axis=1, keepdims=True)
    baseline = np.zeros_like(features)
    valid = norms[:, 0] > 0
    baseline[valid] = features[valid] / norms[valid]

    np.testing.assert_allclose(actual, baseline @ query, rtol=0, atol=1e-6)


def test_top_indices_are_deterministic_for_partial_ties():
    scores = np.array([1.0] * 10 + [0.0] * 15, dtype=np.float32)

    actual = quiltnet_retrieval._top_indices(scores, top_k=1)

    np.testing.assert_array_equal(actual, np.arange(12))


def test_prompt_vectors_batch_only_uncached_prompts(monkeypatch):
    class FakeTorch:
        @staticmethod
        @contextlib.contextmanager
        def inference_mode():
            yield

    class FakeModel:
        def __init__(self):
            self.calls = []

        def encode_text(self, prompts):
            self.calls.append(list(prompts))
            return np.array(
                [[float(index + 1), 1.0] for index, _ in enumerate(prompts)],
                dtype=np.float32,
            )

    class FakeTokenizer:
        def __call__(self, prompts):
            return prompts

    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_model_name", "test-model")
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_prompt_cache_size", 16)
    with quiltnet_retrieval._text_cache_lock:
        quiltnet_retrieval._text_embedding_cache.clear()
        quiltnet_retrieval._text_cache_hits = 0
        quiltnet_retrieval._text_cache_misses = 0

    model = FakeModel()
    tokenizer = FakeTokenizer()
    first = quiltnet_retrieval._encode_text_vectors(
        ["alpha", "beta"], model, tokenizer, "cpu"
    )
    second = quiltnet_retrieval._encode_text_vectors(
        ["beta", "gamma"], model, tokenizer, "cpu"
    )

    assert model.calls == [["alpha", "beta"], ["gamma"]]
    assert first.shape == (2, 2)
    assert second.shape == (2, 2)
    assert quiltnet_retrieval._text_cache_hits == 1
    assert quiltnet_retrieval._text_cache_misses == 3


def test_prepared_features_are_memory_mapped_and_checksum_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quiltnet_retrieval.settings, "research_embedding_cache_dir", str(tmp_path / "cache")
    )
    source = tmp_path / "features.normalized.npy"
    np.save(source, np.eye(3, dtype=np.float32), allow_pickle=False)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()

    features, metadata = quiltnet_retrieval._load_features(
        str(source), expected_sha256=checksum, normalized=True
    )

    assert isinstance(features, np.memmap)
    assert metadata == {"prepared": True, "normalized": True}
    np.testing.assert_array_equal(features, np.eye(3, dtype=np.float32))

    with pytest.raises(quiltnet_retrieval.QuiltNetUnavailable, match="checksum mismatch"):
        quiltnet_retrieval._load_features(
            str(source), expected_sha256="0" * 64, normalized=True
        )


def test_artifact_cache_prunes_oldest_unprotected_file(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    old = cache_dir / "old.pt"
    protected = cache_dir / "protected.h5"
    newest = cache_dir / "new.npy"
    old.write_bytes(b"1234")
    protected.write_bytes(b"5678")
    newest.write_bytes(b"90ab")
    os.utime(old, (1, 1))
    os.utime(protected, (2, 2))
    os.utime(newest, (3, 3))
    monkeypatch.setattr(quiltnet_retrieval.settings, "research_embedding_cache_dir", str(cache_dir))
    monkeypatch.setattr(quiltnet_retrieval.settings, "research_embedding_cache_max_bytes", 8)

    quiltnet_retrieval._prune_embedding_cache({protected})

    assert not old.exists()
    assert protected.exists()
    assert newest.exists()


def test_prepared_search_preserves_score_order(monkeypatch, tmp_path):
    class FakeTorch:
        @staticmethod
        @contextlib.contextmanager
        def inference_mode():
            yield

    class FakeModel:
        def encode_text(self, prompts):
            return np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (len(prompts), 1))

    features_path = tmp_path / "features.npy"
    coordinates_path = tmp_path / "coordinates.npy"
    np.save(
        features_path,
        np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]], dtype=np.float32),
        allow_pickle=False,
    )
    np.save(
        coordinates_path,
        np.array([[0.0, 0.0], [50000.0, 0.0], [0.0, 50000.0]], dtype=np.float32),
        allow_pickle=False,
    )
    artifact = {
        "features_uri": str(features_path),
        "coordinates_uri": str(coordinates_path),
        "features_sha256": hashlib.sha256(features_path.read_bytes()).hexdigest(),
        "coordinates_sha256": hashlib.sha256(coordinates_path.read_bytes()).hexdigest(),
        "shape": [3, 2],
        "coordinates_shape": [3, 2],
        "dtype": "float32",
        "coordinates_dtype": "float32",
        "normalized": True,
    }
    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)
    monkeypatch.setattr(quiltnet_retrieval, "_load_model", lambda: (FakeModel(), lambda prompts: prompts, "cpu"))
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_model_name", "prepared-test")
    monkeypatch.setattr(quiltnet_retrieval.settings, "quiltnet_score_chunk_size", 2)
    monkeypatch.setattr(
        quiltnet_retrieval.settings,
        "research_embedding_cache_dir",
        str(tmp_path / "cache"),
    )
    with quiltnet_retrieval._text_cache_lock:
        quiltnet_retrieval._text_embedding_cache.clear()

    regions = quiltnet_retrieval.QuiltNetRetriever().search(
        model_id="quiltnet_pmb",
        model_record={"serving_artifact": artifact, "patch_size": 100},
        query_plan={"primary": "tumor", "positive": ["tumor"], "negative": []},
        top_k=2,
        slide_width=100000,
        slide_height=100000,
    )

    assert [region["rank"] for region in regions] == [1, 2]
    assert regions[0]["candidate_id"] != regions[1]["candidate_id"]
    assert regions[0]["score"] > regions[1]["score"]
