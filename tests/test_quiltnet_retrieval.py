from types import SimpleNamespace

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
