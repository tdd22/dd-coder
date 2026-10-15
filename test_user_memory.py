import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.tool import REGISTRY
from agent.tool import validate_arguments
from agent.user_memory import (
    StalePreferenceUpdateError,
    UserMemory,
    UserMemoryValidationError,
    UserScopeError,
    WriteStatus,
)


def memory(root: Path, user_id: str = "local-user") -> UserMemory:
    return UserMemory(root, user_id=user_id)


class UserMemoryTest(unittest.TestCase):
    def test_profile_patch_refreshes_projection_and_leaves_other_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            store = memory(Path(folder))
            created = store.update_profile({"call_them": "  老  王  ", "city": "上海"})
            self.assertEqual(created.changed, ("call_them", "city"))
            again = store.update_profile({"call_them": "老 王", "city": None})
            self.assertEqual(again.changed, ("city",))
            self.assertEqual(again.unchanged, ("call_them",))
            self.assertEqual(store.read_profile(), {"call_them": "老 王"})
            text = store.user_path.read_text(encoding="utf-8")
            self.assertIn("Call them: 老 王", text)
            self.assertNotIn("上海", text)

    def test_profile_rejects_unknown_field_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            store = memory(Path(folder))
            with self.assertRaises(UserMemoryValidationError):
                store.update_profile({"nickname": "老王"})
            self.assertFalse(store.profile_path.exists())

    def test_preference_create_retry_and_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            store = memory(Path(folder))
            created = store.set_preference(
                " Response.Language ",
                " 中文 ",
                updated_at="2026-10-01T00:00:00Z",
            )
            self.assertEqual(created.status, WriteStatus.CREATED)
            self.assertEqual(created.revision, 1)
            same = store.set_preference(
                "response.language",
                "中文",
                updated_at="2026-10-02T00:00:00Z",
                source_event_id="session-1:e1",
            )
            self.assertEqual(same.status, WriteStatus.UPDATED)
            retry = store.set_preference(
                "response.language",
                "中文",
                updated_at="2026-10-03T00:00:00Z",
                source_event_id="session-1:e1",
            )
            self.assertEqual(retry.status, WriteStatus.UNCHANGED)
            self.assertEqual(retry.revision, 2)
            replaced = store.set_preference(
                "response.language",
                "English",
                updated_at="2026-10-04T00:00:00Z",
                source_event_id="session-1:e2",
            )
            self.assertEqual(replaced.status, WriteStatus.UPDATED)
            self.assertEqual(replaced.previous_value, "中文")
            self.assertEqual(replaced.revision, 3)
            self.assertIn("`response.language`: English", store.memory_path.read_text(encoding="utf-8"))

    def test_expired_preference_stays_in_json_and_leaves_context(self):
        with tempfile.TemporaryDirectory() as folder:
            store = memory(Path(folder))
            store.set_preference(
                "response.detail",
                "verbose during onboarding",
                updated_at="2026-08-01T01:00:00Z",
                expires_at="2026-08-02T01:00:00Z",
            )
            store.set_preference(
                "response.language",
                "中文",
                updated_at="2026-08-01T01:00:00Z",
            )
            later = datetime(2026, 10, 15, tzinfo=timezone.utc)
            context = store.get_context_for_agent(as_of=later)
            self.assertIn("response.language", context)
            self.assertNotIn("response.detail", context)
            saved = store.list_preferences()
            self.assertEqual(
                [item.key for item in saved],
                ["response.detail", "response.language"],
            )

    def test_delete_blocks_old_evidence_and_allows_later_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            store = memory(Path(folder))
            store.set_preference(
                "response.language",
                "中文",
                updated_at="2026-10-01T00:00:00Z",
            )
            deleted = store.delete_preference(
                "response.language",
                deleted_at="2026-10-15T13:00:00Z",
            )
            self.assertEqual(deleted.status, WriteStatus.DELETED)
            self.assertEqual(deleted.previous_value, "中文")
            self.assertNotIn("中文", store.preferences_path.read_text(encoding="utf-8"))
            again = store.delete_preference("response.language")
            self.assertEqual(again.status, WriteStatus.UNCHANGED)
            with self.assertRaises(StalePreferenceUpdateError):
                store.set_preference(
                    "response.language",
                    "English",
                    updated_at="2026-10-01T00:00:00Z",
                )
            self.assertEqual(store.list_preferences(), [])
            restored = store.set_preference(
                "response.language",
                "English",
                updated_at="2026-10-16T00:00:00Z",
            )
            self.assertEqual(restored.status, WriteStatus.CREATED)
            self.assertEqual(restored.revision, 2)
            self.assertEqual(store.list_deleted_preferences(), [])

    def test_users_stay_apart_and_wrong_scope_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            alice = memory(root, "alice")
            bob = memory(root, "bob")
            alice.set_preference("editor.indent", "tabs", updated_at="2026-10-01T00:00:00Z")
            bob.set_preference("editor.indent", "spaces", updated_at="2026-10-01T00:00:00Z")
            self.assertEqual(alice.list_preferences()[0].value, "tabs")
            self.assertEqual(bob.list_preferences()[0].value, "spaces")
            alice.update_profile({"name": "Alice"})
            bob.profile_path.write_text(
                alice.profile_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaises(UserScopeError):
                bob.read_profile()

    def test_context_contains_profile_and_not_an_empty_file(self):
        with tempfile.TemporaryDirectory() as folder:
            store = memory(Path(folder))
            self.assertEqual(store.get_context_for_agent(), "")
            store.update_profile({"call_them": "老王"})
            text = store.get_context_for_agent()
            self.assertIn("## User profile", text)
            self.assertIn("Call them: 老王", text)
            self.assertNotIn("workspace", text.casefold())

    def test_null_deletes_a_profile_field_and_preference_value_rejects_null(self):
        preference = REGISTRY._specs["save_user_preference"]
        rejected = validate_arguments(
            preference.parameters,
            {"key": "response.language", "value": None},
        )
        self.assertIn("必须是字符串", rejected)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("agent.user_memory.USER_MEMORY_ROOT", root):
                loaded: set[str] = set()
                REGISTRY.dispatch(
                    root,
                    "tool_search",
                    {"names": ["update_user_profile"]},
                    loaded,
                )
                REGISTRY.dispatch(
                    root,
                    "defer_execute",
                    {
                        "name": "update_user_profile",
                        "arguments": {"call_them": "老王", "city": "上海"},
                    },
                    loaded,
                    approver=lambda decision: True,
                )
                deleted = REGISTRY.dispatch(
                    root,
                    "defer_execute",
                    {
                        "name": "update_user_profile",
                        "arguments": {"city": None},
                    },
                    loaded,
                    approver=lambda decision: True,
                )
            store = UserMemory(root)
            self.assertEqual(deleted, "档案已更新: changed=city unchanged=-")
            self.assertEqual(store.read_profile(), {"call_them": "老王"})
            self.assertNotIn("上海", store.user_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
