import unittest

from probe import compare_export, normalize_updates, report_rows


class NormalizeUpdatesTests(unittest.TestCase):
    def test_extracts_message_reply_edit_and_anonymized_actor(self):
        updates = [
            {"update_id": 7, "message": {
                "message_id": 13, "date": 1790000000,
                "chat": {"id": -1001234567890, "type": "supergroup"},
                "from": {"id": 42, "username": "private_name"},
                "text": "synthetic text", "reply_to_message": {"message_id": 12},
            }},
            {"update_id": 8, "edited_message": {
                "message_id": 13, "date": 1790000000, "edit_date": 1790000005,
                "chat": {"id": -1001234567890, "type": "supergroup"},
                "from": {"id": 42, "username": "private_name"},
                "text": "synthetic edit",
            }},
        ]
        rows = normalize_updates(updates)
        self.assertEqual([row["event_type"] for row in rows], ["message", "edited_message"])
        self.assertEqual(rows[0]["message_id"], 13)
        self.assertEqual(rows[0]["reply_to_message_id"], 12)
        self.assertEqual(rows[1]["edit_date"], 1790000005)
        self.assertEqual(rows[0]["chat_key"], rows[1]["chat_key"])
        self.assertEqual(rows[0]["actor_key"], rows[1]["actor_key"])
        self.assertNotIn("private_name", repr(rows))
        self.assertNotIn("synthetic text", repr(rows))
        self.assertNotIn("synthetic", repr(report_rows(rows)))
        self.assertNotIn('"message_id"', repr(report_rows(rows)))
        self.assertNotIn('"update_id"', repr(report_rows(rows)))
        self.assertIn("message_key", repr(report_rows(rows)))

    def test_keeps_membership_changes_without_exposing_usernames(self):
        updates = [{"update_id": 9, "my_chat_member": {
            "chat": {"id": -1001234567890, "type": "supergroup"},
            "from": {"id": 42, "username": "private_name"},
            "old_chat_member": {"status": "member"},
            "new_chat_member": {"status": "left"},
            "date": 1790000010,
        }}]
        row = normalize_updates(updates)[0]
        self.assertEqual(row["event_type"], "my_chat_member")
        self.assertEqual(row["old_status"], "member")
        self.assertEqual(row["new_status"], "left")
        self.assertNotIn("private_name", repr(row))

    def test_export_comparison_requires_timestamp_reply_and_text_to_match(self):
        updates = normalize_updates([{"update_id": 4, "message": {
            "message_id": 10, "date": 1790000000,
            "chat": {"id": -1001234567890, "type": "supergroup"},
            "from": {"id": 42}, "text": "synthetic",
        }}])
        export = [{"chat_key": updates[0]["chat_key"], "message_id": 10, "reply_to_message_id": None,
                   "actor_key": updates[0]["actor_key"], "date": 1790000000,
                   "text_sha256": updates[0]["text_sha256"]}]
        self.assertEqual(compare_export(export, updates)[0]["match_status"], "matched")
        export[0]["date"] += 1
        self.assertEqual(compare_export(export, updates)[0]["match_status"], "ambiguous")
        export[0]["date"] -= 1
        export[0]["chat_key"] = "different-per-run-chat-key"
        result = compare_export(export, updates)[0]
        self.assertEqual(result["match_status"], "chat_id_mismatch")
        self.assertFalse(result["chat_id_exact_match"])


if __name__ == "__main__":
    unittest.main()
