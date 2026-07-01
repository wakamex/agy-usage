from __future__ import annotations

import json
import os
import io
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import agy_usage


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


class AgyUsageTests(unittest.TestCase):
    def test_parse_quota_summary_groups(self):
        summary = agy_usage._parse_quota_summary(
            {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "description": "Models within this group: Gemini Flash, Gemini Pro",
                        "buckets": [
                            {
                                "bucketId": "gemini-5h",
                                "displayName": "Five Hour Limit",
                                "window": "5h",
                                "remainingFraction": 0.625,
                                "resetTime": "2026-06-30T04:00:00Z",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(summary["groups"][0]["display_name"], "Gemini Models")
        self.assertEqual(summary["groups"][0]["buckets"][0]["display_name"], "Five Hour Limit")
        self.assertEqual(summary["groups"][0]["buckets"][0]["remaining_pct"], 62.5)

    def test_statusline_uses_gemini_five_hour_remaining(self):
        reset_time = (datetime.now(UTC) + timedelta(minutes=7)).isoformat()
        data = {
            "quota_summary": {
                "groups": [
                    {
                        "display_name": "Gemini Models",
                        "buckets": [
                            {"display_name": "Weekly Limit", "window": "weekly", "remaining_pct": 95.0},
                            {
                                "display_name": "Five Hour Limit",
                                "window": "5h",
                                "remaining_pct": 62.5,
                                "reset_time": reset_time,
                            },
                        ],
                    },
                    {
                        "display_name": "Claude and GPT models",
                        "buckets": [
                            {"display_name": "Five Hour Limit", "window": "5h", "remaining_pct": 100.0}
                        ],
                    },
                ]
            },
            "model": "Gemini Test",
        }

        statusline = agy_usage._statusline_text(data)

        self.assertIn("q:62.5%left", statusline)
        self.assertIn("reset:", statusline)
        self.assertIn("model:Gemini_Test", statusline)

    def test_subscription_plan_prefers_paid_tier(self):
        plan = agy_usage._parse_subscription_plan(
            {
                "currentTier": {"name": "Antigravity"},
                "paidTier": {"name": "Google AI Pro"},
            }
        )

        self.assertEqual(plan, "Google AI Pro")

    def test_fetch_quota_summary_uses_antigravity_project(self):
        summary_response = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [{"displayName": "Five Hour Limit", "remainingFraction": 0.5}],
                }
            ]
        }

        with (
            mock.patch.object(agy_usage, "get_access_token", return_value="token"),
            mock.patch.object(
                agy_usage,
                "_code_assist_post",
                side_effect=[
                    {
                        "cloudaicompanionProject": "healthy-shore-gs5kt",
                        "paidTier": {"name": "Google AI Pro"},
                    },
                    summary_response,
                ],
            ) as post_mock,
        ):
            summary = agy_usage.fetch_quota_summary()

        self.assertEqual(summary["project_id"], "healthy-shore-gs5kt")
        self.assertEqual(summary["plan"], "Google AI Pro")
        self.assertEqual(summary["source"], "quota_summary_api")
        self.assertEqual(summary["groups"][0]["buckets"][0]["remaining_pct"], 50.0)
        self.assertEqual(
            post_mock.mock_calls,
            [
                mock.call(
                    "loadCodeAssist",
                    {"metadata": {"ideType": "ANTIGRAVITY"}},
                    "token",
                ),
                mock.call(
                    "retrieveUserQuotaSummary",
                    {"project": "healthy-shore-gs5kt"},
                    "token",
                ),
            ],
        )

    def test_print_status_reports_quota_summary_error(self):
        data = {
            "project_root": "/code/agy-usage",
            "quota_summary_error": "HTTP Error 403: Forbidden",
            "history": {"entries": 0},
        }

        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            agy_usage._print_status(data)

        output = stdout.getvalue()
        self.assertIn("Quota summary", output)
        self.assertIn("HTTP Error 403: Forbidden", output)

    def test_read_history_summary_counts_commands_and_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            history.write_text(
                json.dumps({"display": "/usage", "timestamp": 1_000, "workspace": "/code/a"}) + "\n"
                + "not json\n"
                + json.dumps({"display": "/model", "timestamp": 2_000, "workspace": "/code/a"}) + "\n"
                + json.dumps({"display": "/usage", "timestamp": 3_000, "workspace": "/code/b"}) + "\n"
            )

            summary = agy_usage.read_history_summary(history)

        self.assertEqual(summary["entries"], 3)
        self.assertEqual(summary["top_commands"]["/usage"], 2)
        self.assertEqual(summary["top_workspaces"]["/code/a"], 2)
        self.assertEqual(summary["latest_at"], "1970-01-01T00:00:03+00:00")

    def test_access_token_can_come_from_env(self):
        with mock.patch.dict(os.environ, {"AGY_ACCESS_TOKEN": "token"}, clear=True):
            self.assertEqual(agy_usage.get_access_token(), "token")

    def test_access_token_reads_nested_antigravity_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "antigravity-oauth-token"
            _write_json(
                token_file,
                {
                    "auth_method": "consumer",
                    "token": {
                        "access_token": "nested-token",
                        "token_type": "Bearer",
                    },
                },
            )

            with (
                mock.patch.object(agy_usage, "TOKEN_FILE", token_file),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(agy_usage.get_access_token(), "nested-token")

    def test_expired_access_token_refreshes_and_persists_nested_token_file(self):
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "access_token": "fresh-token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                ).encode()

        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "antigravity-oauth-token"
            _write_json(
                token_file,
                {
                    "auth_method": "consumer",
                    "token": {
                        "access_token": "stale-token",
                        "token_type": "Bearer",
                        "refresh_token": "refresh-token",
                        "expiry": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                    },
                },
            )

            with (
                mock.patch.object(agy_usage, "TOKEN_FILE", token_file),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    agy_usage,
                    "_oauth_client_candidates",
                    return_value=[("client-id", "client-secret")],
                ),
                mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen_mock,
            ):
                token = agy_usage.get_access_token()

            self.assertEqual(token, "fresh-token")
            written = json.loads(token_file.read_text())
            self.assertEqual(written["token"]["access_token"], "fresh-token")
            self.assertEqual(written["token"]["refresh_token"], "refresh-token")
            self.assertIn("client_secret", urlopen_mock.call_args.args[0].data.decode())

    def test_oauth_client_candidates_find_user_bin_when_path_is_sparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agy_bin = home / ".local" / "bin" / "agy"
            agy_bin.parent.mkdir(parents=True)
            client_id = "1071006060591-test.apps" + ".googleusercontent.com"
            client_secret = "GO" + "CSPX-" + "abcdefghijklmnopqrstuvwxyz12"
            agy_bin.write_bytes(client_id.encode() + b"\0" + client_secret.encode())
            agy_bin.chmod(0o755)

            with (
                mock.patch.object(agy_usage.Path, "home", return_value=home),
                mock.patch.dict(os.environ, {"PATH": ""}, clear=True),
            ):
                candidates = agy_usage._oauth_client_candidates()

        self.assertEqual(
            candidates,
            [(client_id, client_secret)],
        )

    def test_fetch_quota_summary_refreshes_once_on_auth_error(self):
        http_error = urllib.error.HTTPError(
            url="https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )
        summary_response = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [{"displayName": "Five Hour Limit", "remainingFraction": 0.75}],
                }
            ]
        }

        with (
            mock.patch.object(
                agy_usage,
                "get_access_token",
                side_effect=["stale-token", "fresh-token"],
            ) as token_mock,
            mock.patch.object(
                agy_usage,
                "_code_assist_post",
                side_effect=[
                    http_error,
                    {"cloudaicompanionProject": "healthy-shore-gs5kt"},
                    summary_response,
                ],
            ),
        ):
            summary = agy_usage.fetch_quota_summary()

        self.assertEqual(summary["groups"][0]["buckets"][0]["remaining_pct"], 75.0)
        token_mock.assert_has_calls([mock.call(), mock.call(force_refresh=True)])

    def test_build_usage_json_includes_history_and_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            history = tmp_path / "history.jsonl"
            settings = tmp_path / "settings.json"
            _write_json(settings, {"model": "Gemini Test"})
            history.write_text(json.dumps({"display": "/usage", "timestamp": 1_000}) + "\n")

            with (
                mock.patch.object(agy_usage, "HISTORY_FILE", history),
                mock.patch.object(agy_usage, "SETTINGS_FILE", settings),
                mock.patch.object(agy_usage, "fetch_quota_summary", return_value={"plan": "Google AI Pro"}),
            ):
                usage = agy_usage.build_usage_json(tmp_path)

        self.assertEqual(usage["model"], "Gemini Test")
        self.assertEqual(usage["plan"], "Google AI Pro")
        self.assertIn("history", usage["source"])
        self.assertNotIn("quota_summary_error", usage)
        self.assertNotIn("account_quota", usage)

    def test_force_refresh_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            project_root.mkdir()
            usage_file = tmp_path / "usage-limits.json"
            cached = {
                "project_root": str(project_root.resolve()),
                "source": ["quota_summary_api"],
                "updated_at": datetime.now(UTC).isoformat(),
            }
            fresh = {
                "project_root": str(project_root.resolve()),
                "source": ["quota_summary_api"],
                "updated_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            }
            usage_file.write_text(json.dumps(cached) + "\n")

            with (
                mock.patch.object(agy_usage, "DEFAULT_USAGE_FILE", usage_file),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(agy_usage, "build_usage_json", return_value=fresh) as build_mock,
            ):
                result = agy_usage._get_cached_usage(project_root=project_root)
                self.assertEqual(result, cached)
                build_mock.assert_not_called()

                result = agy_usage._get_cached_usage(project_root=project_root, force_refresh=True)

            self.assertEqual(result, fresh)
            self.assertEqual(json.loads(usage_file.read_text()), fresh)


if __name__ == "__main__":
    unittest.main()
