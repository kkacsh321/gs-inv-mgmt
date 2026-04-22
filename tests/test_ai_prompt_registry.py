import unittest
from types import SimpleNamespace

from app.services.ai_prompt_registry import (
    active_prompt_version,
    create_prompt_version,
    list_prompt_versions,
    prompt_keys_for_workflow,
    restore_prompt_version,
)


class _RepoStub:
    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}

    def get_runtime_setting(self, environment, key, active_only=True):
        return self.rows.get(str(key))

    def upsert_runtime_setting(
        self,
        *,
        environment,
        key,
        value,
        value_type="str",
        description="",
        is_active=True,
        actor="system",
    ):
        row = SimpleNamespace(
            key=str(key),
            value=str(value),
            value_type=str(value_type),
            description=str(description),
            is_active=bool(is_active),
            actor=str(actor),
        )
        self.rows[str(key)] = row
        return row


class AIPromptRegistryTests(unittest.TestCase):
    def test_create_list_restore_listing_prompt_version(self) -> None:
        repo = _RepoStub()
        repo.upsert_runtime_setting(
            environment="local",
            key="listing_wizard_ai_system_message",
            value="sys-v1",
            value_type="str",
            description="",
            is_active=True,
            actor="admin",
        )
        repo.upsert_runtime_setting(
            environment="local",
            key="listing_wizard_ai_instruction_template",
            value="instr-v1",
            value_type="str",
            description="",
            is_active=True,
            actor="admin",
        )
        repo.upsert_runtime_setting(
            environment="local",
            key="listing_wizard_ai_seed_default",
            value="seed-v1",
            value_type="str",
            description="",
            is_active=True,
            actor="admin",
        )

        created = create_prompt_version(repo, "listing", actor="admin", note="baseline")
        self.assertTrue(str(created.get("version_id") or "").strip())
        self.assertEqual(active_prompt_version(repo, "listing"), created.get("version_id"))

        # mutate current prompts, then restore
        repo.upsert_runtime_setting(
            environment="local",
            key="listing_wizard_ai_system_message",
            value="sys-v2",
            value_type="str",
            description="",
            is_active=True,
            actor="admin",
        )
        restored = restore_prompt_version(
            repo,
            "listing",
            version_id=str(created.get("version_id") or ""),
            actor="admin",
        )
        self.assertIsNotNone(restored)
        self.assertEqual(
            str(repo.get_runtime_setting("local", "listing_wizard_ai_system_message").value),
            "sys-v1",
        )

    def test_comp_registry_limit_and_keys(self) -> None:
        repo = _RepoStub()
        for key in prompt_keys_for_workflow("comp"):
            repo.upsert_runtime_setting(
                environment="local",
                key=key,
                value=f"{key}-v1",
                value_type="str",
                description="",
                is_active=True,
                actor="admin",
            )
        create_prompt_version(repo, "comp", actor="admin", note="first", max_versions=2)
        repo.upsert_runtime_setting(
            environment="local",
            key="comp_llm_system_message",
            value="changed",
            value_type="str",
            description="",
            is_active=True,
            actor="admin",
        )
        create_prompt_version(repo, "comp", actor="admin", note="second", max_versions=2)
        repo.upsert_runtime_setting(
            environment="local",
            key="comp_llm_instruction_template",
            value="changed-again",
            value_type="str",
            description="",
            is_active=True,
            actor="admin",
        )
        create_prompt_version(repo, "comp", actor="admin", note="third", max_versions=2)
        rows = list_prompt_versions(repo, "comp", limit=10)
        self.assertLessEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()

