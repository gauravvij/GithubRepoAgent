"""
utils/db.py - Database management layer for the Todo application.

Provides a simple SQLite-backed DatabaseManager class used by app.py
to persist and retrieve todo items.
"""

import sqlite3
import logging

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages SQLite database operations for todo items.

    Used by app.py as the persistence layer. All CRUD operations
    for the todos table are encapsulated here.
    """

    def __init__(self, db_path: str = "todos.db"):
        """
        Initialize the DatabaseManager with a SQLite database path.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path

    def initialize(self) -> None:
        """Create the todos table if it does not already exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
        logger.info("Database initialized at: %s", self.db_path)

    def fetch_all(self) -> list:
        """
        Retrieve all todo items from the database.

        Returns:
            List of dicts with keys: id, title, description, created_at.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM todos ORDER BY created_at DESC")
            return [dict(row) for row in cursor.fetchall()]

    def insert(self, title: str, description: str = "") -> int:
        """
        Insert a new todo item into the database.

        Args:
            title: The title of the todo item.
            description: Optional description of the todo item.

        Returns:
            The auto-generated ID of the newly inserted row.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO todos (title, description) VALUES (?, ?)",
                (title, description),
            )
            conn.commit()
            return cursor.lastrowid

    def delete(self, todo_id: int) -> bool:
        """
        Delete a todo item by its ID.

        Args:
            todo_id: The integer ID of the todo to delete.

        Returns:
            True if a row was deleted, False if no matching row was found.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            conn.commit()
            return cursor.rowcount > 0
