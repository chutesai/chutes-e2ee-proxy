from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.mark.skipif(os.name == "nt", reason="install.sh is unix-only")
def test_install_sh_uv_required_blocks_pipx_fallback(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    log_file = tmp_path / "calls.log"
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")

    _write_executable(
        local_bin / "uv",
        "#!/usr/bin/env bash\n"
        'echo \"uv $*\" >> \"$TEST_LOG\"\n'
        "exit 1\n",
    )
    _write_executable(
        local_bin / "pipx",
        "#!/usr/bin/env bash\n"
        'echo \"pipx $*\" >> \"$TEST_LOG\"\n'
        "exit 0\n",
    )
    _write_executable(
        local_bin / "chutes-e2ee-proxy",
        "#!/usr/bin/env bash\n"
        'echo \"proxy $*\" >> \"$TEST_LOG\"\n'
        "exit 0\n",
    )

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["TEST_LOG"] = str(log_file)
    env["CHUTES_PROXY_UV_REQUIRED"] = "1"
    env["CHUTES_PROXY_TUNNEL"] = "off"
    env["CHUTES_TLS_CERT_FILE"] = str(cert_file)
    env["CHUTES_TLS_KEY_FILE"] = str(key_file)

    result = subprocess.run(
        ["bash", "install.sh", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    log_text = log_file.read_text() if log_file.exists() else ""
    assert "uv tool install" in log_text
    assert "pipx " not in log_text
    assert "proxy " not in log_text


@pytest.mark.skipif(os.name == "nt", reason="install.sh is unix-only")
def test_install_sh_uv_failure_falls_back_to_pipx_when_not_required(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    log_file = tmp_path / "calls.log"
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")

    _write_executable(
        local_bin / "uv",
        "#!/usr/bin/env bash\n"
        'echo \"uv $*\" >> \"$TEST_LOG\"\n'
        "exit 1\n",
    )
    _write_executable(
        local_bin / "pipx",
        "#!/usr/bin/env bash\n"
        'echo \"pipx $*\" >> \"$TEST_LOG\"\n'
        "exit 0\n",
    )
    _write_executable(
        local_bin / "chutes-e2ee-proxy",
        "#!/usr/bin/env bash\n"
        'echo \"proxy $*\" >> \"$TEST_LOG\"\n'
        "exit 0\n",
    )

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["TEST_LOG"] = str(log_file)
    env["CHUTES_PROXY_TUNNEL"] = "off"
    env["CHUTES_TLS_CERT_FILE"] = str(cert_file)
    env["CHUTES_TLS_KEY_FILE"] = str(key_file)

    result = subprocess.run(
        ["bash", "install.sh", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    log_text = log_file.read_text()
    assert "uv tool install" in log_text
    assert "pipx install --force" in log_text


@pytest.mark.skipif(os.name == "nt", reason="install.sh is unix-only")
def test_install_sh_sets_cloudflared_origin_ca_pool_from_mkcert(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    log_file = tmp_path / "calls.log"
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")

    caroot = tmp_path / "mkcert-caroot"
    caroot.mkdir()
    root_ca = caroot / "rootCA.pem"
    root_ca.write_text("root-ca")

    _write_executable(
        local_bin / "uv",
        "#!/usr/bin/env bash\n"
        'echo "uv $*" >> "$TEST_LOG"\n'
        "exit 0\n",
    )
    _write_executable(
        local_bin / "mkcert",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-CAROOT" ]; then\n'
        '  echo "$TEST_CAROOT"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _write_executable(
        local_bin / "chutes-e2ee-proxy",
        "#!/usr/bin/env bash\n"
        'echo "origin_ca=${CHUTES_CLOUDFLARED_ORIGIN_CA_POOL:-}" >> "$TEST_LOG"\n'
        "exit 0\n",
    )

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["TEST_LOG"] = str(log_file)
    env["TEST_CAROOT"] = str(caroot)
    env["CHUTES_PROXY_TUNNEL"] = "off"
    env["CHUTES_TLS_CERT_FILE"] = str(cert_file)
    env["CHUTES_TLS_KEY_FILE"] = str(key_file)

    result = subprocess.run(
        ["bash", "install.sh", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    log_text = log_file.read_text()
    assert f"origin_ca={root_ca}" in log_text
