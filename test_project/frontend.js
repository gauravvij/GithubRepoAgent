/**
 * frontend.js - Browser-side JavaScript client for the Todo API.
 *
 * Fetches todo items from the Flask backend (app.py) and renders
 * them into the DOM. Also handles form submission to create new todos.
 * Reads API base URL from config (mirrors config.json api.base_url).
 */

const API_BASE = "/api/v1";

/**
 * Fetch all todos from the backend and render them into #todo-list.
 * @returns {Promise<void>}
 */
async function loadTodos() {
  try {
    const response = await fetch(`${API_BASE}/todos`);
    if (!response.ok) {
      throw new Error(`HTTP error: ${response.status}`);
    }
    const data = await response.json();
    renderTodos(data.items);
  } catch (err) {
    console.error("Failed to load todos:", err);
  }
}

/**
 * Render an array of todo objects into the #todo-list element.
 * @param {Array<{id: number, title: string, description: string}>} todos
 */
function renderTodos(todos) {
  const list = document.getElementById("todo-list");
  list.innerHTML = "";
  todos.forEach((todo) => {
    const li = document.createElement("li");
    li.dataset.id = todo.id;
    li.innerHTML = `
      <strong>${escapeHtml(todo.title)}</strong>
      <span>${escapeHtml(todo.description || "")}</span>
      <button onclick="deleteTodo(${todo.id})">Delete</button>
    `;
    list.appendChild(li);
  });
}

/**
 * Submit a new todo item to the backend via POST /todos.
 * @param {Event} event - The form submit event.
 */
async function createTodo(event) {
  event.preventDefault();
  const titleInput = document.getElementById("todo-title");
  const descInput = document.getElementById("todo-desc");

  const payload = {
    title: titleInput.value.trim(),
    description: descInput.value.trim(),
  };

  try {
    const response = await fetch(`${API_BASE}/todos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const err = await response.json();
      alert(`Error: ${err.error}`);
      return;
    }
    titleInput.value = "";
    descInput.value = "";
    await loadTodos();
  } catch (err) {
    console.error("Failed to create todo:", err);
  }
}

/**
 * Delete a todo item by ID via DELETE /todos/:id.
 * @param {number} todoId - The ID of the todo to delete.
 */
async function deleteTodo(todoId) {
  try {
    const response = await fetch(`${API_BASE}/todos/${todoId}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      console.error("Failed to delete todo:", todoId);
      return;
    }
    await loadTodos();
  } catch (err) {
    console.error("Delete error:", err);
  }
}

/**
 * Escape HTML special characters to prevent XSS.
 * @param {string} str - Raw string to escape.
 * @returns {string} HTML-safe string.
 */
function escapeHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Initialize on DOM ready
document.addEventListener("DOMContentLoaded", () => {
  loadTodos();
  const form = document.getElementById("todo-form");
  if (form) {
    form.addEventListener("submit", createTodo);
  }
});
