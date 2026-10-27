import hashlib
import io
from pathlib import Path
import sqlite3
import types
from unittest.mock import Mock, patch

from rich.console import Console

import memory
from test_memory_prompt import IsolatedPromptFixture, ada


class MemoryValidationTests(IsolatedPromptFixture):
    def setUp(self):
        super().setUp()
        self.remember("té", category="preferences", key="bebida")

    def snapshot(self):
        return hashlib.sha256(Path(memory.DB_PATH).read_bytes()).hexdigest()

    def command(self, text, content):
        client = Mock()
        client.chat.completions.create.return_value = types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        )
        output = io.StringIO()
        with patch.object(ada, "console", Console(file=output, color_system=None)):
            result = ada.execute_memory_command(text, client, "test-model")
        return result, output.getvalue(), client

    def test_save_normalizes_defaults_and_returns_committed_records(self):
        saved = memory.save_memories([{"key": " edad\n", "value": " 30\r "}])
        self.assertEqual(saved, [{"category": "general", "key": "edad", "value": "30"}])
        self.assertEqual(memory.get_all_memories(), [
            ("preferences", "bebida", "té"), ("general", "edad", "30")
        ])

    def test_save_preserves_upsert_and_record_order(self):
        memory.save_memories([{"category": "gustos", "key": "bebida", "value": "café"}])
        self.assertEqual(memory.get_all_memories(), [("gustos", "bebida", "café")])

    def test_save_rejects_invalid_batches_without_changing_database(self):
        invalid = [None, {}, "text", [], [None], ["text"], [{}]]
        for field in ("key", "value", "category"):
            for value in (None, False, 0, 42, [], {}, "", " \r\n "):
                record = {"category": "general", "key": "edad", "value": "30"}
                record[field] = value
                invalid.append([record])
        invalid.append([{"key": "edad"}])
        invalid.append([{"value": "30"}])
        invalid.append([{"key": "edad", "value": "30"}, {"key": "otra", "value": ""}])
        invalid.append([{"key": "bebida", "value": "changed"}, {"key": "otra", "value": ""}])
        for batch in invalid:
            with self.subTest(batch=batch):
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    memory.save_memories(batch)
                self.assertEqual(self.snapshot(), before)

    def test_invalid_input_does_not_initialize_database(self):
        fresh = self.workspace / "not-created.db"
        with patch.object(memory, "DB_PATH", str(fresh)):
            for operation, batch in ((memory.save_memories, [{}]), (memory.delete_memories, [5])):
                with self.assertRaises(ValueError):
                    operation(batch)
                self.assertFalse(fresh.exists())

    def test_delete_rejects_invalid_batches_without_changing_database(self):
        for batch in (None, {}, "bebida", [None], [1], [False], [[]], [{}], [""], [" \n"], ["bebida", 1]):
            with self.subTest(batch=batch):
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    memory.delete_memories(batch)
                self.assertEqual(self.snapshot(), before)

    def test_delete_preserves_exact_matching_and_reports_actual_changes(self):
        before = self.snapshot()
        self.assertEqual(memory.delete_memories(["bebi", "missing"]), [])
        self.assertEqual(memory.delete_memories([]), [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(memory.delete_memories([" bebida\n", "bebida"]), ["bebida"])
        self.assertEqual(memory.get_all_memories(), [])

    def test_sqlite_failure_rolls_back_entire_save_and_delete_batches(self):
        for operation, event, batch in (
            (memory.save_memories, "INSERT", [{"key": "new", "value": "ok"}, {"key": "blocked", "value": "no"}]),
            (memory.save_memories, "UPDATE", [{"key": "bebida", "value": "changed"}, {"key": "blocked", "value": "no"}]),
            (memory.delete_memories, "DELETE", ["bebida", "blocked"]),
        ):
            with self.subTest(event=event):
                memory.save_memories([{"key": "blocked", "value": "keep"}])
                row = "NEW" if event == "INSERT" else "OLD"
                with memory.get_db_connection() as conn:
                    conn.execute(f"""CREATE TRIGGER fail_{event} BEFORE {event} ON memories
                        WHEN {row}.key = 'blocked' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
                before = self.snapshot()
                with self.assertRaises(sqlite3.IntegrityError):
                    operation(batch)
                self.assertEqual(self.snapshot(), before)
                with memory.get_db_connection() as conn:
                    conn.execute(f"DROP TRIGGER fail_{event}")

    def test_commit_failure_rolls_back_existing_record_update(self):
        with memory.get_db_connection() as conn:
            conn.executescript("""
                CREATE TABLE parent(id INTEGER PRIMARY KEY);
                CREATE TABLE child(id INTEGER REFERENCES parent(id)
                    DEFERRABLE INITIALLY DEFERRED);
                CREATE TRIGGER fail_commit AFTER UPDATE ON memories
                BEGIN INSERT INTO child VALUES (99); END;
            """)
        original_connect = memory.get_db_connection

        def foreign_keys_enabled():
            conn = original_connect()
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

        before = self.snapshot()
        with patch.object(memory, "get_db_connection", foreign_keys_enabled):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
                memory.save_memories([{"key": "bebida", "value": "changed"}])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(memory.get_all_memories(), [("preferences", "bebida", "té")])

    def test_save_command_confirms_only_valid_persisted_records(self):
        result, output, _ = self.command("Anota mi edad", '[{"key":"edad","value":"30"}]')
        self.assertEqual(result, "Información guardada.")
        self.assertIn("Anotado: edad -> 30", output)
        self.assertIn(("general", "edad", "30"), memory.get_all_memories())

    def test_invalid_save_responses_leave_data_unchanged_without_success_output(self):
        for content in (None, "not JSON", "[", "[]", "[{}]", '[{"key":"edad","value":3}]',
                        '[{"key":"edad","value":"30"},{}]', '{"records":[{"key":"edad","value":"30"}]}'):
            with self.subTest(content=content):
                before = self.snapshot()
                result, output, _ = self.command("Guarda mi edad", content)
                self.assertEqual(result, "Hubo un error al intentar extraer y guardar la información.")
                self.assertNotIn("Anotado:", output)
                self.assertEqual(self.snapshot(), before)

    def test_delete_command_validates_entire_selection_against_offered_keys(self):
        for content in ('["missing"]', '["bebida","missing"]', '["bebida",1]', '[{}]', '[""]', '"bebida"', 'null', 'not JSON'):
            with self.subTest(content=content):
                before = self.snapshot()
                result, output, client = self.command("Borra mi bebida", content)
                self.assertEqual(result, "Hubo un error al procesar el borrado de memoria.")
                self.assertNotIn("Olvidado:", output)
                self.assertEqual(self.snapshot(), before)
                self.assertIn("bebida", client.chat.completions.create.call_args.kwargs["messages"][0]["content"])

    def test_delete_command_confirms_actual_deletion(self):
        result, output, _ = self.command("Olvida mi bebida", '["bebida"]')
        self.assertEqual(result, "Información eliminada de mi base de datos.")
        self.assertIn("Olvidado: bebida", output)
        self.assertEqual(memory.get_all_memories(), [])

    def test_no_match_is_not_reported_as_deletion(self):
        before = self.snapshot()
        result, output, _ = self.command("Borra mi edad", "[]")
        self.assertEqual(result, "No encontré ese dato en mi memoria para borrarlo.")
        self.assertNotIn("Olvidado:", output)
        self.assertEqual(self.snapshot(), before)
        with patch.object(memory, "delete_memories", return_value=[]):
            result, output, _ = self.command("Borra mi bebida", '["bebida"]')
        self.assertEqual(result, "No encontré ese dato en mi memoria para borrarlo.")
        self.assertNotIn("Olvidado:", output)
        self.assertEqual(self.snapshot(), before)
        with memory.get_db_connection() as conn:
            conn.execute("CREATE TRIGGER keep BEFORE DELETE ON memories BEGIN SELECT RAISE(IGNORE); END")
        before = self.snapshot()
        result, output, _ = self.command("Olvida mi bebida", '["bebida"]')
        self.assertEqual(result, "No encontré ese dato en mi memoria para borrarlo.")
        self.assertNotIn("Olvidado:", output)
        self.assertEqual(self.snapshot(), before)

    def test_database_failure_never_reports_command_success(self):
        for text, content, helper, expected in (
            ("Anota mi edad", '[{"key":"edad","value":"30"}]', "save_memories",
             "Hubo un error al intentar extraer y guardar la información."),
            ("Borra mi bebida", '["bebida"]', "delete_memories", "Hubo un error al procesar el borrado de memoria."),
        ):
            with self.subTest(helper=helper):
                before = self.snapshot()
                with patch.object(memory, helper, side_effect=sqlite3.OperationalError("test failure")):
                    result, output, _ = self.command(text, content)
                self.assertEqual(result, expected)
                self.assertNotIn("Anotado:", output)
                self.assertNotIn("Olvidado:", output)
                self.assertEqual(self.snapshot(), before)

    def test_existing_wrapped_json_and_command_prefixes_still_work(self):
        for prefix in ("Anota", "Guarda", "Recuerda", "Memoriza", "Por favor guarda", "Puedes anota"):
            with self.subTest(prefix=prefix):
                result, _, _ = self.command(prefix + " mi edad", '```json\n[{"key":"edad","value":"30"}]\n```')
                self.assertEqual(result, "Información guardada.")
        for prefix in ("Borra", "Olvida", "Elimina", "Por favor borra", "Puedes olvida"):
            with self.subTest(prefix=prefix):
                self.remember("té", key="bebida")
                result, _, _ = self.command(prefix + " mi bebida", 'Claves: ["bebida"]')
                self.assertEqual(result, "Información eliminada de mi base de datos.")

    def test_non_command_and_empty_memory_keep_existing_behavior(self):
        before = self.snapshot()
        result, output, client = self.command("¿Qué recuerdas de mi bebida?", "[]")
        self.assertIsNone(result)
        self.assertEqual(output, "")
        client.chat.completions.create.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        memory.delete_memories(["bebida"])
        result, output, client = self.command("Borra mi edad", "[]")
        self.assertEqual(result, "Mi memoria ya está vacía.")
        self.assertEqual(output, "")
        client.chat.completions.create.assert_not_called()
