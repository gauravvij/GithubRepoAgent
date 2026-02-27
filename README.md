# Query Any Repo

A terminal-styled AI agent that analyzes any GitHub repository and answers questions about its codebase. Point it at any public GitHub URL and get a comprehensive architectural breakdown — then ask follow-up questions in a conversational interface.

## What It Does

- **Clones any GitHub repository** into a temporary directory
- **Scans the entire codebase** recursively, ignoring binaries, `.git`, `node_modules`, etc.
- **Generates a structured analysis report** covering:
  - Directory hierarchy and file inventory
  - Key components, modules, and their interactions
  - Dependency and import relationships
  - Architectural patterns and entry points
  - Data flow across the project
- **Answers follow-up questions** about the codebase in a multi-turn conversational interface
- **Streams progress** in real-time showing map/reduce pipeline stages

## Tech Stack

- **LLM**: Configurable via `.env` (default: `google/gemini-2.5-flash-lite` via OpenRouter)
- **Backend**: Python + Flask with Server-Sent Events (SSE) for streaming
- **Analysis**: Parallel map-reduce chunking with token-aware hierarchical reduction
- **Frontend**: Terminal-styled dark web UI

## Getting Started

### 1. Clone the repo

```bash
git clone https://github.com/gauravvij/query-any-repo.git
cd query-any-repo
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and add your OpenRouter API key
```

### 4. Run the server

```bash
python app.py
```

Open your browser at `http://localhost:5000`

## Usage

1. Paste any public GitHub repository URL into the input field
2. Click **Analyze** — the agent clones the repo and runs the map-reduce analysis pipeline
3. View the structured report in the terminal-styled interface
4. Ask follow-up questions about the codebase in the chat input below

## Configuration

All configuration is done via `.env` (see `.env.example`):

| Variable | Description | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | Your OpenRouter API key | *(required)* |
| `OPENROUTER_BASE_URL` | OpenRouter API base URL | `https://openrouter.ai/api/v1` |
| `MODEL_NAME` | LLM model to use | `google/gemini-2.5-flash-lite-preview-06-17` |
| `FLASK_HOST` | Server bind host | `0.0.0.0` |
| `FLASK_PORT` | Server port | `5000` |
| `GITHUB_CLONE_BASE` | Temp dir for cloned repos | `/tmp/codebase_agent_repos` |

You can swap in any OpenRouter-compatible model by changing `MODEL_NAME` in `.env` — the UI reflects the active model dynamically.

## Architecture

```
app.py              # Flask server, SSE streaming endpoints
agent.py            # Parallel map-reduce LLM analysis pipeline
scanner.py          # Recursive directory scanner and file parser
github_utils.py     # GitHub repo cloning utility
templates/
  index.html        # Terminal-styled web UI
```

### Analysis Pipeline

1. **Scan** — recursively traverse the cloned repo, collect all text-based source files
2. **Chunk** — split large codebases into token-safe chunks (well under 128k tokens each)
3. **Map** — summarize each chunk in parallel using `ThreadPoolExecutor`
4. **Reduce** — hierarchically merge summaries (token-aware, capped at 100k tokens per call)
5. **Report** — produce final structured analysis
6. **Q&A** — answer follow-up questions with full report context preserved

## License

MIT
