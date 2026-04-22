import unittest

from app.services.slack_ops_runner import (
    _normalize_command_text,
    _resolve_role,
    _role_map,
    _strip_mention_prefix,
)


class SlackOpsRunnerHelpersTests(unittest.TestCase):
    def test_strip_mention_prefix(self) -> None:
        self.assertEqual(
            _strip_mention_prefix("<@U123> comp silver dollar", bot_user_id="U123"),
            "comp silver dollar",
        )
        self.assertEqual(
            _strip_mention_prefix("comp silver dollar", bot_user_id="U123"),
            "comp silver dollar",
        )

    def test_normalize_command_text_applies_optional_prefix(self) -> None:
        self.assertEqual(
            _normalize_command_text("<@U123> gs comp morgan", bot_user_id="U123", command_prefix="gs"),
            "comp morgan",
        )
        self.assertEqual(
            _normalize_command_text("<@U123> comp morgan", bot_user_id="U123", command_prefix=""),
            "comp morgan",
        )

    def test_role_map_and_resolve_role(self) -> None:
        mapping = _role_map("U123:admin,keith:ops,badpair")
        self.assertEqual(mapping.get("u123"), "admin")
        self.assertEqual(mapping.get("keith"), "ops")
        self.assertEqual(
            _resolve_role(
                slack_user_id="U123",
                slack_username="someone",
                fallback_role="viewer",
                role_map=mapping,
            ),
            "admin",
        )
        self.assertEqual(
            _resolve_role(
                slack_user_id="U000",
                slack_username="keith",
                fallback_role="viewer",
                role_map=mapping,
            ),
            "ops",
        )
        self.assertEqual(
            _resolve_role(
                slack_user_id="U000",
                slack_username="nobody",
                fallback_role="invalid-role",
                role_map=mapping,
            ),
            "viewer",
        )


if __name__ == "__main__":
    unittest.main()

