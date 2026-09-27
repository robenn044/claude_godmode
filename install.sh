#!/usr/bin/env bash
# Install the /godmode skill for Claude Code (macOS / Linux / WSL / Git Bash).
#
#   ./install.sh                 # global: ~/.claude/skills/godmode  (every project)
#   ./install.sh --project       # this project only: ./.claude/skills/godmode
#   ./install.sh --dir PATH      # custom skills directory
#   ./install.sh --uninstall     # remove it (combine with --project/--dir)
#
# Remote one-liner (no clone needed):
#   curl -fsSL https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.sh | bash
set -euo pipefail

REPO="robenn044/claude_godmode"
REF="${GODMODE_REF:-main}"
TARGET_ROOT="${HOME}/.claude/skills"
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --project)   TARGET_ROOT="$(pwd)/.claude/skills" ;;
    --dir)       TARGET_ROOT="$2"; shift ;;
    --ref)       REF="$2"; shift ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)   sed -n '2,12p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

DEST="${TARGET_ROOT}/godmode"

if [ "$UNINSTALL" = 1 ]; then
  rm -rf "$DEST"
  echo "Removed ${DEST}"
  exit 0
fi

# Locate the skill source: the local checkout if we're in one, otherwise download it.
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
TMP=""
cleanup() { [ -n "$TMP" ] && rm -rf "$TMP"; }
trap cleanup EXIT

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/skills/godmode/SKILL.md" ]; then
  SRC="$SCRIPT_DIR/skills/godmode"
else
  TMP="$(mktemp -d)"
  echo "Downloading ${REPO}@${REF} ..."
  if command -v git >/dev/null 2>&1; then
    git clone --quiet --depth 1 --branch "$REF" "https://github.com/${REPO}.git" "$TMP/src"
  else
    mkdir -p "$TMP/src"
    curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/${REF}" | tar -xz -C "$TMP/src" --strip-components=1
  fi
  SRC="$TMP/src/skills/godmode"
fi

mkdir -p "$TARGET_ROOT"
rm -rf "$DEST"
cp -R "$SRC" "$DEST"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "$DEST/scripts/godmode_engine.py"

echo "Installed /godmode -> ${DEST}"

# Pre-flight checks
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "WARNING: Python 3.8+ not found. Install it; the swarm engine needs it." >&2
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "WARNING: 'claude' CLI not on PATH. Install Claude Code: npm install -g @anthropic-ai/claude-code" >&2
  echo "         (without it /godmode falls back to in-session subagents, max 50 agents)" >&2
elif [ -n "$PY" ]; then
  "$PY" "$DEST/scripts/godmode_engine.py" check || true
fi

cat <<MSG

Done. Restart Claude Code (or open a new session), then run for example:

  /godmode 10 Find the root cause of the flaky test in tests/api and propose a fix

MSG
