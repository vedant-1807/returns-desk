#!/usr/bin/env bash
# Start an Ollama server for this project on port 11435.
#
# It reads the default model folder (~/.ollama/models), which on this machine holds
# llama3.1:8b and nomic-embed-text. The Ollama app's own server on port 11434 is left
# alone. Stop this one with Ctrl-C.
set -euo pipefail

export OLLAMA_HOST="127.0.0.1:${RD_OLLAMA_PORT:-11435}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/.ollama/models}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-15m}"

echo "Ollama for Returns Desk on http://$OLLAMA_HOST (models: $OLLAMA_MODELS)"
exec ollama serve
