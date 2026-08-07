from argparse import Namespace

from tools import run_local_snapshot_wsi_server as runner


def test_local_runner_disables_access_logs(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        runner,
        "_parse_args",
        lambda: Namespace(host="127.0.0.1", port=8081),
    )
    monkeypatch.setattr(
        runner.uvicorn,
        "run",
        lambda app, **kwargs: calls.update(app=app, **kwargs),
    )

    assert runner.main() == 0
    assert calls["access_log"] is False
