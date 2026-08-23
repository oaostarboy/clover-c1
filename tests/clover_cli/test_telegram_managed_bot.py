"""Tests for clover_cli.telegram_managed_bot — QR codes, deep links, pairing."""

from __future__ import annotations

from pathlib import PureWindowsPath
from unittest.mock import MagicMock, patch

from clover_cli.telegram_managed_bot import (
    DEFAULT_MANAGER_BOT,
    TELEGRAM_ONBOARDING_URL_ENV,
    TelegramBotSetupResult,
    TelegramPairing,
    create_pairing,
    generate_bot_username,
    generate_deep_link,
    generate_pairing_nonce,
    poll_for_setup_result,
    poll_for_token,
    print_qr_code,
    render_qr_terminal,
)


VALID_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
SECOND_VALID_TOKEN = "987654321:abcdefghijklmnopqrstuvwxyzABCDEF"


class TestGenerateBotUsername:
    def test_secure_default_format(self):
        name = generate_bot_username()
        assert name.startswith("clover_")
        assert name.endswith("_bot")
        assert len(name) == len("clover_") + 16 + len("_bot")
        assert len(name) <= 32


    def test_uniqueness(self):
        names = {generate_bot_username() for _ in range(20)}
        assert len(names) == 20


class TestGenerateDeepLink:
    def test_basic_format(self):
        link = generate_deep_link(
            manager_bot="TestBot",
            suggested_username="my_bot",
        )
        assert link == "https://t.me/newbot/TestBot/my_bot"


    def test_name_url_encoded(self):
        link = generate_deep_link(
            manager_bot="Bot",
            suggested_username="test_bot",
            suggested_name="Clover & Friends",
        )
        assert "Clover+%26+Friends" in link


class TestPairingNonce:

    def test_hex_chars(self):
        nonce = generate_pairing_nonce()
        assert all(c in "0123456789abcdef" for c in nonce)


class TestQRCode:
    def test_render_returns_string(self):
        result = render_qr_terminal("https://example.com")
        if result:
            assert isinstance(result, str)
            assert len(result) > 10

    def test_render_graceful_without_qrcode(self):
        with patch.dict("sys.modules", {"qrcode": None}):
            render_qr_terminal("https://example.com")

    def test_print_qr_code_with_url(self, capsys):
        print_qr_code("https://t.me/newbot/Bot/test_bot")
        captured = capsys.readouterr()
        assert "https://t.me/newbot/Bot/test_bot" in captured.out


class TestCreatePairing:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "pairing_id": "abcdefghijklmnop",
            "poll_token": "secret-token",
            "suggested_username": "clover_abcdefghijklmnop_bot",
            "deep_link": "https://t.me/newbot/CloverSetupBot/clover_abcdefghijklmnop_bot?name=Clover+Agent",
            "qr_payload": "https://t.me/newbot/CloverSetupBot/clover_abcdefghijklmnop_bot?name=Clover+Agent",
            "expires_at": "2026-05-18T00:00:00.000Z",
        }

        with patch(
            "clover_cli.telegram_managed_bot.httpx.post", return_value=mock_resp
        ) as post:
            pairing = create_pairing("https://api.example.com", bot_name="Clover Cognition")

        assert pairing == TelegramPairing(
            pairing_id="abcdefghijklmnop",
            poll_token="secret-token",
            suggested_username="clover_abcdefghijklmnop_bot",
            deep_link="https://t.me/newbot/CloverSetupBot/clover_abcdefghijklmnop_bot?name=Clover+Agent",
            qr_payload="https://t.me/newbot/CloverSetupBot/clover_abcdefghijklmnop_bot?name=Clover+Agent",
            expires_at="2026-05-18T00:00:00.000Z",
        )
        post.assert_called_once_with(
            "https://api.example.com/v1/telegram/pairings",
            json={"bot_name": "Clover Cognition"},
            timeout=10.0,
        )

    def test_failure_status(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch(
            "clover_cli.telegram_managed_bot.httpx.post", return_value=mock_resp
        ):
            assert create_pairing("https://api.example.com") is None


    def test_uses_env_override(self, monkeypatch):
        monkeypatch.setenv(TELEGRAM_ONBOARDING_URL_ENV, "https://worker.example")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch(
            "clover_cli.telegram_managed_bot.httpx.post", return_value=mock_resp
        ) as post:
            create_pairing()
        assert post.call_args.args[0] == "https://worker.example/v1/telegram/pairings"


class TestPollForToken:
    def pairing(self):
        return TelegramPairing(
            pairing_id="abcdefghijklmnop",
            poll_token="secret-token",
            suggested_username="clover_abcdefghijklmnop_bot",
            deep_link="https://t.me/newbot/CloverSetupBot/clover_abcdefghijklmnop_bot",
            qr_payload="https://t.me/newbot/CloverSetupBot/clover_abcdefghijklmnop_bot",
        )

    def test_immediate_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "bot_username": "clover_abcdefghijklmnop_bot",
            "owner_user_id": 42,
            "status": "ready",
            "token": VALID_TOKEN,
        }

        with patch(
            "clover_cli.telegram_managed_bot.httpx.get", return_value=mock_resp
        ) as get:
            with patch("clover_cli.telegram_managed_bot.time.sleep"):
                token = poll_for_token(
                    "https://api.example.com", self.pairing(), timeout=5
                )

        assert token == VALID_TOKEN
        assert (
            get.call_args.args[0]
            == "https://api.example.com/v1/telegram/pairings/abcdefghijklmnop"
        )
        assert get.call_args.kwargs["headers"] == {
            "Authorization": "Bearer secret-token"
        }




    def test_eventual_success(self):
        not_ready = MagicMock()
        not_ready.status_code = 200
        not_ready.json.return_value = {"status": "waiting"}

        ready = MagicMock()
        ready.status_code = 200
        ready.json.return_value = {"status": "ready", "token": SECOND_VALID_TOKEN}

        call_count = 0

        def fake_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return not_ready
            return ready

        with patch("clover_cli.telegram_managed_bot.httpx.get", side_effect=fake_get):
            with patch("clover_cli.telegram_managed_bot.time.sleep"):
                token = poll_for_token(
                    "https://api.example.com", self.pairing(), timeout=30
                )
                assert token == SECOND_VALID_TOKEN


class TestSetupTelegramAuto:
    def test_setup_helper_exists(self):
        from clover_cli.setup import _setup_telegram_auto

        assert callable(_setup_telegram_auto)

    def test_setup_result_passes_profile_name_for_profile_home(self, monkeypatch, tmp_path):
        from clover_cli import setup

        seen = {}
        profile_home = tmp_path / ".clover" / "profiles" / "oracle"
        profile_home.mkdir(parents=True)

        monkeypatch.setattr(setup, "get_clover_home", lambda: profile_home)

        def fake_auto_setup_telegram_bot_result(*, profile_name=None):
            seen["profile_name"] = profile_name
            return None

        monkeypatch.setattr(
            "clover_cli.telegram_managed_bot.auto_setup_telegram_bot_result",
            fake_auto_setup_telegram_bot_result,
        )

        assert setup._setup_telegram_auto_result() is None
        assert seen["profile_name"] == "oracle"

    def test_profile_name_from_home_path_handles_windows_separators(self):
        from clover_cli.setup import _profile_name_from_clover_home

        assert (
            _profile_name_from_clover_home(
                PureWindowsPath(r"C:\Users\test\AppData\Local\clover\profiles\oracle")
            )
            == "oracle"
        )
