# 🐬 dolphin_ai

Local LLM-powered security knowledge base chatbot.  
Runs entirely on your machine — no cloud required.

## What it does

- Chat with your security documents and Obsidian notes via RAG
- 6 chat modes: `general`, `rag`, `ctf`, `pentest`, `bypass`, `threat`
- HyDE query expansion + hybrid dense/BM25 search + Flashrank reranking
- Automated pipeline: indexes new notes, generates summaries, detects knowledge gaps
- All data stays local (Qdrant + PostgreSQL + Ollama)

## Architecture

```
Browser → FastAPI (port 7777) → Ollama (local LLM)
                              → Qdrant (vector search)
                              → PostgreSQL (chat history)
                              → Obsidian vault (read-only)
```

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose)
- [Ollama](https://ollama.ai) running locally on port 11434

## Recommended models

Pull these in Ollama before starting:

```bash
# Embedding model (required)
ollama pull nomic-embed-text

# Chat model — uncensored, security-focused (recommended)
ollama pull hf.co/mradermacher/Qwen2.5-Coder-14B-Instruct-Uncensored-GGUF:Q4_K_M

# Lighter alternative (8B, faster on lower VRAM)
ollama pull hf.co/mradermacher/Qwen2.5-Coder-7B-Instruct-Uncensored-GGUF:Q4_K_M

# General-purpose alternative
ollama pull qwen2.5-coder:14b
```

> The default model is `hf.co/mradermacher/Qwen2.5-Coder-14B-Instruct-Uncensored-GGUF:Q4_K_M`.  
> Change `CHAT_MODEL` in `.env` to use any Ollama model.

**Tested on**: Apple Silicon M3 Pro (18GB RAM). Adjust `num_ctx` values in `config/settings.yaml` for your hardware.

## Quick start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/dolphin_ai.git
cd dolphin_ai

# 2. Configure
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD and VAULT_PATH

# 3. Create required directories
mkdir -p data/qdrant data/postgres Storage

# 4. Start
docker compose up -d --build

# 5. Open
open http://localhost:7777
```

## Configuration

| Variable | Description |
|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL password (any strong string) |
| `CHAT_MODEL` | Ollama model tag to use for chat |
| `VAULT_PATH` | Absolute path to your Obsidian vault (or any markdown directory) |
| `DOCKER_PLATFORM` | `linux/arm64` (Apple Silicon) or `linux/amd64` (Intel/Linux) |
| `PERPLEXITY_API_KEY` | Optional — enables knowledge gap enrichment |
| `ANTHROPIC_API_KEY` | Optional — enables knowledge validation |

Edit `config/settings.yaml` to customize:
- LLM context window sizes
- Obsidian vault folder structure
- Domain classification categories
- Processing parameters

## Indexing your own documents

The pipeline automatically indexes your Obsidian vault into Qdrant on startup.

To index additional markdown files manually:

```bash
# Index a directory of markdown files into security_kb
docker run --rm \
  --network dolphin_ai_dolphin-net \
  --add-host host.docker.internal:host-gateway \
  -v "/path/to/your/docs:/guidelines:ro" \
  -e QDRANT_URL=http://qdrant:6333 \
  -e OLLAMA_URL=http://host.docker.internal:11434 \
  dolphin_ai-pipeline:latest \
  python /app/src/index_guidelines.py
```

## Chat modes

| Mode | Description |
|---|---|
| `general` | General security Q&A |
| `rag` | Searches your knowledge base first |
| `ctf` | CTF challenge assistance |
| `pentest` | Penetration testing guidance |
| `bypass` | WAF/filter bypass techniques |
| `threat` | Threat intelligence analysis |

## Services

| Service | Port | Description |
|---|---|---|
| chat | 7777 | Web UI + REST API |
| qdrant | 6333 | Vector database |
| postgres | 5432 | Chat history + metadata |
| pipeline | — | Background indexing scheduler |

## Stopping

```bash
docker compose down        # stop, keep data
docker compose down -v     # stop, delete volumes (fresh start)
```
