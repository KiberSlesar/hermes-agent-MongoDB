"""Tests for gateway linger auto-enable behavior on headless Linux installs."""

from types import SimpleNamespace

import hermes_cli.gateway as gateway


class TestEnsureSystemdLingerEnabled:
    def test_linger_already_enabled_via_file(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr("getpass.getuser", lambda: "testuser")
        monkeypatch.setattr(
            gateway,
            "Path",
            lambda _path: SimpleNamespace(exists=lambda: True),
        )

        calls = []
        monkeypatch.setattr(
            gateway.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs))
        )

        assert gateway.ensure_systemd_linger_enabled() is True

        out = capsys.readouterr().out
        assert "Systemd linger is enabled" in out
        assert calls == []

    def test_loginctl_success_enables_linger(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr("getpass.getuser", lambda: "testuser")
        monkeypatch.setattr(
            gateway, "Path", lambda _path: SimpleNamespace(exists=lambda: False)
        )
        monkeypatch.setattr(
            gateway,
            "get_systemd_linger_status",
            lambda: (False, ""),
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/loginctl")

        enabled_after = {"done": False}

        def fake_is_enabled(username):
            return enabled_after["done"]

        def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
            if cmd[:2] == ["loginctl", "enable-linger"]:
                enabled_after["done"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway, "_systemd_linger_is_enabled", fake_is_enabled)
        monkeypatch.setattr(gateway.subprocess, "run", fake_run)

        assert gateway.ensure_systemd_linger_enabled() is True

        out = capsys.readouterr().out
        assert "Enabling linger" in out
        assert "Linger enabled" in out

    def test_loginctl_failure_tries_sudo_and_shows_guidance(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr("getpass.getuser", lambda: "testuser")
        monkeypatch.setattr(
            gateway, "Path", lambda _path: SimpleNamespace(exists=lambda: False)
        )
        monkeypatch.setattr(
            gateway,
            "get_systemd_linger_status",
            lambda: (False, ""),
        )
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/loginctl" if name == "loginctl" else "/usr/bin/sudo",
        )
        monkeypatch.setattr(gateway, "_systemd_linger_is_enabled", lambda _user: False)
        monkeypatch.setattr(
            gateway.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=1, stdout="", stderr="Permission denied"
            ),
        )

        assert gateway.ensure_systemd_linger_enabled() is False

        out = capsys.readouterr().out
        assert "sudo loginctl enable-linger testuser" in out
        assert "Permission denied" in out

    def test_quiet_mode_suppresses_output(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr("getpass.getuser", lambda: "testuser")
        monkeypatch.setattr(gateway, "_systemd_linger_is_enabled", lambda _user: True)

        assert gateway.ensure_systemd_linger_enabled(quiet=True) is True
        assert capsys.readouterr().out == ""


class TestEnsureLingerEnabled:
    def test_delegates_to_shared_helper(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            gateway,
            "ensure_systemd_linger_enabled",
            lambda **kwargs: calls.append(kwargs) or True,
        )
        gateway._ensure_linger_enabled()
        assert calls == [{}]


def test_systemd_install_calls_linger_helper(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "systemd" / "user" / "hermes-gateway.service"

    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: unit_path)
    monkeypatch.setattr(
        gateway,
        "generate_systemd_unit",
        lambda system=False, run_as_user=None: (
            '[Service]\nEnvironment="HERMES_HOME=/home/alice/.hermes"\n'
        ),
    )

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append((cmd, check))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    helper_calls = []
    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    monkeypatch.setattr(
        gateway, "_ensure_linger_enabled", lambda: helper_calls.append(True)
    )

    gateway.systemd_install(force=False)

    out = capsys.readouterr().out
    assert unit_path.exists()
    assert [cmd for cmd, _ in calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", gateway.get_service_name()],
    ]
    assert helper_calls == [True]
    assert "User service installed and enabled" in out


def test_systemd_install_existing_unit_still_checks_linger(
    monkeypatch, tmp_path, capsys
):
    unit_path = tmp_path / "systemd" / "user" / "hermes-gateway.service"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("[Service]\nExecStart=/usr/bin/hermes gateway run\n", encoding="utf-8")

    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: unit_path)
    monkeypatch.setattr(gateway, "systemd_unit_is_current", lambda system=False: True)

    helper_calls = []
    monkeypatch.setattr(
        gateway, "_ensure_linger_enabled", lambda: helper_calls.append(True)
    )

    gateway.systemd_install(force=False)

    out = capsys.readouterr().out
    assert "Service already installed" in out
    assert helper_calls == [True]
