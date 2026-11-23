import unittest
import socket
import urllib.error
import json
import tempfile
from pathlib import Path

from probe import (
    compare_export,
    numeric_id,
    normalize_export_chat_id,
    normalize_export,
    normalize_updates,
    report_rows,
    safe_network_error,
    valid_bot_token,
)


class NormalizeUpdatesTests(unittest.TestCase):
    def test_normalizes_export_chat_ids_to_bot_api_dialog_ids(self):
        self.assertEqual(normalize_export_chat_id(456, "private_group"), -456)
        self.assertEqual(normalize_export_chat_id("789", "private_supergroup"), -1000000000789)
        self.assertEqual(normalize_export_chat_id(-456, "private_group"), -456)
        self.assertEqual(normalize_export_chat_id(-1000000000789, "private_supergroup"), -1000000000789)
        self.assertIsNone(normalize_export_chat_id(-456, "private_supergroup"))
        self.assertIsNone(normalize_export_chat_id(-1000000000789, "private_group"))
        self.assertIsNone(normalize_export_chat_id(-456, None))
        self.assertEqual(normalize_export_chat_id(999999999999, "private_group"), -999999999999)
        self.assertEqual(normalize_export_chat_id(997852516352, "private_supergroup"), -1997852516352)
        self.assertIsNone(normalize_export_chat_id(1000000000000, "private_group"))
        self.assertIsNone(numeric_id(456.9))
        self.assertIsNone(numeric_id(True))
        self.assertEqual(numeric_id("-456"), -456)

    def test_export_ids_are_normalized_before_comparison(self):
        updates = normalize_updates([{"update_id": 1, "message": {
            "message_id": 1, "date": 1790000000,
            "chat": {"id": -456, "type": "group"},
            "from": {"id": 42}, "text": "synthetic event",
        }}])
        export_data = {"id": "456", "type": "private_group", "messages": [{
            "id": "1", "type": "message", "from_id": "user42",
            "date_unixtime": "1790000000", "text": "synthetic event",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            export_path = Path(directory) / "result.json"
            export_path.write_text(json.dumps(export_data), encoding="utf-8")
            exported = normalize_export(export_path)
        result = compare_export(exported, updates)[0]
        self.assertEqual(result["match_status"], "matched")
        self.assertFalse(result["chat_id_exact_match"])
        self.assertTrue(result["chat_id_converted_match"])
        self.assertTrue(result["message_id_equal"])

    def test_rejects_control_characters_in_a_bot_token(self):
        self.assertTrue(valid_bot_token("123456789:Abc_def-987"))
        self.assertFalse(valid_bot_token("\x16"))
        self.assertFalse(valid_bot_token("123456789:Abc def"))

    def test_network_error_classification_does_not_echo_details(self):
        error = urllib.error.URLError(socket.gaierror("private network detail"))
        self.assertEqual(safe_network_error(error), "DNS lookup for Telegram failed.")
        self.assertNotIn("private network detail", safe_network_error(error))
        decode_error = UnicodeDecodeError("utf-8", b"private-response-bytes", 0, 1, "invalid start byte")
        self.assertEqual(safe_network_error(decode_error), "Telegram endpoint returned an invalid response.")
        self.assertNotIn("private-response-bytes", safe_network_error(decode_error))

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
                   "chat_id_value": updates[0]["chat_id_value"],
                   "raw_chat_id_value": updates[0]["chat_id_value"],
                   "actor_key": updates[0]["actor_key"], "date": 1790000000,
                   "text_sha256": updates[0]["text_sha256"]}]
        self.assertEqual(compare_export(export, updates)[0]["match_status"], "matched")
        export[0]["date"] += 1
        result = compare_export(export, updates)[0]
        self.assertEqual(result["match_status"], "ambiguous")
        self.assertTrue(result["chat_id_exact_match"])
        export[0]["date"] -= 1
        export[0]["chat_key"] = "different-per-run-chat-key"
        result = compare_export(export, updates)[0]
        self.assertEqual(result["match_status"], "chat_id_mismatch")
        self.assertFalse(result["chat_id_exact_match"])


if __name__ == "__main__":
    unittest.main()
