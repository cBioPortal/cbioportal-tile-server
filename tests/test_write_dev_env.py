import os

from app.auth import decode_source_seal_key
from tools.write_dev_env import _write_secure, main


def test_write_secure_creates_private_file(tmp_path):
    output = tmp_path / ".env"
    _write_secure(output, "TOKEN=secret\n")

    assert output.read_text(encoding="utf-8") == "TOKEN=secret\n"
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_main_writes_credentials_without_printing_them(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text(
        "[ecs]\nendpoint_url=http://ecs\naws_access_key_id=access\n"
        "aws_secret_access_key=secret\n",
        encoding="utf-8",
    )
    (home / ".databrickscfg").write_text(
        "[DEFAULT]\nhost=https://dbc\ntoken=token\n", encoding="utf-8"
    )
    output = tmp_path / ".env"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WSI_SOURCE_SEAL_KEY", raising=False)

    main(["--output", str(output)])

    captured = capsys.readouterr()
    assert "access" not in captured.out + captured.err
    assert "secret" not in captured.out + captured.err
    assert "token" not in captured.out + captured.err
    contents = output.read_text(encoding="utf-8")
    assert "AWS_SECRET_ACCESS_KEY=secret" in contents
    assert "DATABRICKS" not in contents
    assert (
        "THUMBNAIL_MANIFEST_URI=s3://mskmind-bkt/wsi-thumbnails-dev/manifest.json"
        in contents
    )
    seal_key = next(
        line.split("=", 1)[1]
        for line in contents.splitlines()
        if line.startswith("WSI_SOURCE_SEAL_KEY=")
    )
    assert len(decode_source_seal_key(seal_key)) == 32


def test_main_writes_source_seal_key_from_environment(tmp_path, monkeypatch):
    key = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WSI_SOURCE_SEAL_KEY", key)
    output = tmp_path / ".env"

    main(["--output", str(output)])

    assert f"WSI_SOURCE_SEAL_KEY={key}\n" in output.read_text(encoding="utf-8")
