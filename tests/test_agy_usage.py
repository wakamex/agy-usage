from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import agy_usage


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


class AgyUsageTests(unittest.TestCase):
    def test_parse_quota_buckets_computes_usage_and_limit(self):
        buckets = agy_usage._parse_quota_buckets(
            [
                {
                    "modelId": "gemini-test",
                    "remainingAmount": "25",
                    "remainingFraction": 0.5,
                    "resetTime": "2026-06-30T01:00:00Z",
                }
            ]
        )

        self.assertEqual(buckets[0]["model"], "gemini-test")
        self.assertEqual(buckets[0]["remaining"], 25)
        self.assertEqual(buckets[0]["limit"], 50)
        self.assertEqual(buckets[0]["used_pct"], 50)

    def test_summary_bucket_skips_disabled_and_picks_highest_used(self):
        quota = {
            "buckets": [
                {"model": "disabled", "used_pct": 99, "disabled": True},
                {"model": "flash", "used_pct": 10},
                {"model": "pro", "used_pct": 75},
            ]
        }

        self.assertEqual(agy_usage._select_summary_bucket(quota)["model"], "pro")

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

    def test_build_usage_json_includes_history_when_quota_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            history = tmp_path / "history.jsonl"
            settings = tmp_path / "settings.json"
            _write_json(settings, {"model": "Gemini Test"})
            history.write_text(json.dumps({"display": "/usage", "timestamp": 1_000}) + "\n")

            with (
                mock.patch.object(agy_usage, "HISTORY_FILE", history),
                mock.patch.object(agy_usage, "SETTINGS_FILE", settings),
                mock.patch.object(
                    agy_usage,
                    "fetch_quota_summary",
                    side_effect=RuntimeError("no quota summary"),
                ),
                mock.patch.object(agy_usage, "fetch_quota", side_effect=RuntimeError("no quota")),
            ):
                usage = agy_usage.build_usage_json(tmp_path)

        self.assertEqual(usage["model"], "Gemini Test")
        self.assertIn("history", usage["source"])
        self.assertEqual(usage["quota_summary_error"], "no quota summary")
        self.assertEqual(usage["quota_error"], "no quota")

    def test_force_refresh_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_root = tmp_path / "project"
            project_root.mkdir()
            usage_file = tmp_path / "usage-limits.json"
            cached = {
                "project_root": str(project_root.resolve()),
                "source": ["quota_summary_rpc"],
                "updated_at": datetime.now(UTC).isoformat(),
            }
            fresh = {
                "project_root": str(project_root.resolve()),
                "source": ["quota_summary_rpc"],
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
