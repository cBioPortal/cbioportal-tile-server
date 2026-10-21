import json

import numpy as np

from tools.prepare_quiltnet_artifacts import prepare


def test_prepare_writes_normalized_contiguous_serving_record(tmp_path):
    features_source = tmp_path / "features.npy"
    coordinates_source = tmp_path / "coordinates.npy"
    np.save(
        features_source,
        np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float64),
        allow_pickle=False,
    )
    np.save(
        coordinates_source,
        np.array([[10.0, 20.0, 99.0], [30.0, 40.0, 98.0]], dtype=np.float64),
        allow_pickle=False,
    )

    record = prepare(
        str(features_source),
        str(coordinates_source),
        tmp_path / "prepared",
        artifact_prefix="s3://bucket/serving/slide-1",
    )

    prepared = np.load(tmp_path / "prepared" / "features.normalized.npy", allow_pickle=False)
    coordinates = np.load(tmp_path / "prepared" / "coordinates.npy", allow_pickle=False)
    metadata = json.loads(
        (tmp_path / "prepared" / "serving_artifact.json").read_text(encoding="utf-8")
    )

    np.testing.assert_allclose(prepared[0], [0.6, 0.8])
    np.testing.assert_array_equal(prepared[1], [0.0, 0.0])
    np.testing.assert_array_equal(coordinates, [[10.0, 20.0], [30.0, 40.0]])
    assert prepared.dtype == np.float32
    assert prepared.flags.c_contiguous
    assert coordinates.dtype == np.float32
    assert record["normalized"] is True
    assert record["shape"] == [2, 2]
    assert record["coordinates_shape"] == [2, 2]
    assert metadata == record
