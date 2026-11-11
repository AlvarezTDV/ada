"""MemoryPort bridge retaining the legacy store's validation and transactions."""

from contextlib import contextmanager
import sqlite3

import memory
from ada_core import (
    CapabilityError, CapabilityKind, ErrorCode, ExtensionManifest, MemoryItem,
    SideEffectProfile,
)


class SqliteMemoryAdapter:
    """Use memory.DB_PATH without opening connections or replacing its globals.

    Keys remain globally unique. Category constrains deletion, while recall uses
    the legacy selector's ordering and budget rather than a second ranking rule.
    """

    manifest = ExtensionManifest(
        "memory.sqlite", CapabilityKind.MEMORY,
        SideEffectProfile.NON_IDEMPOTENT_MUTATION,
    )

    @contextmanager
    def _errors(self):
        try:
            yield
        except (ValueError, sqlite3.Error) as error:
            raise CapabilityError(
                ErrorCode.EXECUTION_FAILED, self.manifest.capability,
                "The memory operation failed.",
                extension_id=self.manifest.extension_id,
            ) from error

    def initialize(self) -> None:
        with self._errors():
            memory.init_db()

    def shutdown(self) -> None:
        """No persistent connection is owned by this adapter."""

    def recall(self, query: str) -> tuple[MemoryItem, ...]:
        with self._errors():
            if not isinstance(query, str):
                raise ValueError("query must be a string")
            rows = memory.get_all_memories()
            # Match complete canonical lines, never split user data on ':' or ']'.
            records = {f"- [Tema: {cat}] {key}: {value}": MemoryItem(cat, key, value)
                       for cat, key, value in rows}
            selected = memory.select_memories_for_prompt(query)
            return tuple(records[line] for line in selected.split("\n") if line in records)

    def save(self, item: MemoryItem) -> None:
        with self._errors():
            if not isinstance(item, MemoryItem):
                raise ValueError("item must be a MemoryItem")
            memory.save_memories([{
                "category": item.category, "key": item.key, "value": item.value,
            }])

    def delete(self, category: str, key: str) -> None:
        with self._errors():
            category = memory._validate_text(category, "category")
            key = memory.validate_memory_keys([key])[0]
            if any(cat == category and stored_key == key
                   for cat, stored_key, _ in memory.get_all_memories()):
                memory.delete_memories([key])
