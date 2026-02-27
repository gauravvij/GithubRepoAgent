"""
app.py - Main entry point for the Todo REST API backend.

This Flask application serves as the primary entry point, wiring together
the database layer, business logic, and HTTP routes.
"""

from flask import Flask, jsonify, request
from utils.db import DatabaseManager
from utils.helpers import validate_todo_input, format_response

app = Flask(__name__)
db = DatabaseManager(db_path="todos.db")


@app.route("/todos", methods=["GET"])
def get_todos():
    """Return all todo items from the database."""
    todos = db.fetch_all()
    return jsonify(format_response(todos))


@app.route("/todos", methods=["POST"])
def create_todo():
    """Create a new todo item after validating input."""
    data = request.get_json()
    error = validate_todo_input(data)
    if error:
        return jsonify({"error": error}), 400
    todo_id = db.insert(data["title"], data.get("description", ""))
    return jsonify({"id": todo_id, "status": "created"}), 201


@app.route("/todos/<int:todo_id>", methods=["DELETE"])
def delete_todo(todo_id):
    """Delete a todo item by its ID."""
    deleted = db.delete(todo_id)
    if not deleted:
        return jsonify({"error": "Todo not found"}), 404
    return jsonify({"status": "deleted"}), 200


if __name__ == "__main__":
    db.initialize()
    app.run(debug=True, port=5000)
