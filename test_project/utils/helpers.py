"""
utils/helpers.py - Utility helper functions for the Todo API.

Provides input validation and response formatting utilities consumed
by the main app.py route handlers.
"""


def validate_todo_input(data: dict) -> str:
    """
    Validate the input payload for creating a todo item.

    Args:
        data: Dictionary parsed from the incoming JSON request body.

    Returns:
        An error message string if validation fails, or empty string if valid.
    """
    if not data:
        return "Request body must be valid JSON"
    if "title" not in data:
        return "Field 'title' is required"
    if not isinstance(data["title"], str) or not data["title"].strip():
        return "Field 'title' must be a non-empty string"
    if len(data["title"]) > 200:
        return "Field 'title' must not exceed 200 characters"
    return ""


def format_response(todos: list) -> dict:
    """
    Format a list of todo dicts into a standardized API response envelope.

    Args:
        todos: List of todo item dicts from the database layer.

    Returns:
        A dict with 'count' and 'items' keys suitable for JSON serialization.
    """
    return {
        "count": len(todos),
        "items": todos,
    }
