#!/usr/bin/env bash
set -Eeuo pipefail

# Bootstrap the Codex + Gemini helper setup used for Danish media-stack debugging.
# Safe to rerun. It does not write API keys into this repository.

INSTALL_CODEX=1
RUN_API_TESTS=0
PROMPT_FOR_KEY=1

usage() {
  cat <<'USAGE'
Usage: install-ai-debug-tools.sh [options]

Options:
  --skip-codex       Do not try to install the Codex CLI.
  --run-api-tests    Run live ask-gemini/gemini-write smoke tests.
  --no-prompt        Do not prompt for GEMINI_API_KEY if it is missing.
  -h, --help         Show this help.

Recommended install:
  curl -fsSL -o install-ai-debug-tools.sh https://raw.githubusercontent.com/unknown0152/danish-intelligence/master/scripts/install-ai-debug-tools.sh
  chmod +x install-ai-debug-tools.sh
  ./install-ai-debug-tools.sh

Non-interactive install:
  GEMINI_API_KEY="your_key_here" bash install-ai-debug-tools.sh --run-api-tests

Then start Codex from /root so it reads /root/AGENTS.md.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-codex) INSTALL_CODEX=0 ;;
    --run-api-tests) RUN_API_TESTS=1 ;;
    --no-prompt) PROMPT_FOR_KEY=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

log() {
  printf '[ai-setup] %s\n' "$*"
}

warn() {
  printf '[ai-setup] WARNING: %s\n' "$*" >&2
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

shell_quote() {
  # Print a single-quoted shell literal.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

HOME_DIR="${HOME:-/root}"
BIN_DIR="${HOME_DIR}/bin"
APP_DIR="${HOME_DIR}/.local/share/gemini-tools"
VENV_DIR="${GEMINI_TOOLS_VENV:-${HOME_DIR}/.local/share/gemini-tools-venv}"
ENV_FILE="${GEMINI_WORKER_ENV_FILE:-${HOME_DIR}/.gemini-worker.env}"
AGENTS_FILE="${HOME_DIR}/AGENTS.md"

log "Installing AI debug helper tools for user home: ${HOME_DIR}"

if need_cmd apt-get; then
  log "Installing Debian packages needed by the helpers..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip curl ca-certificates git
  if [ "${INSTALL_CODEX}" = "1" ]; then
    apt-get install -y nodejs npm || warn "Could not install nodejs/npm; Codex install may be skipped."
  fi
else
  warn "apt-get was not found. Assuming python3, venv, pip, curl, git, node, and npm are already installed."
fi

if ! need_cmd python3; then
  echo "python3 is required but was not found." >&2
  exit 1
fi

mkdir -p "${BIN_DIR}" "${APP_DIR}" "$(dirname "${VENV_DIR}")"

if [ ! -x "${VENV_DIR}/bin/python" ]; then
  log "Creating Python virtual environment at ${VENV_DIR}..."
  python3 -m venv "${VENV_DIR}"
fi

log "Installing Python OpenAI client into helper virtualenv..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null
"${VENV_DIR}/bin/python" -m pip install --upgrade openai >/dev/null

log "Writing ask-gemini..."
cat > "${APP_DIR}/ask_gemini.py" <<'PY'
#!/usr/bin/env python3
import argparse
import os
import pathlib
import sys

from openai import OpenAI


parser = argparse.ArgumentParser(description="Delegate bulk file reading and log analysis to Gemini.")
parser.add_argument("--paths", nargs="+", required=True, help="Files to read")
parser.add_argument("--question", required=True, help="Question to ask about the files")
args = parser.parse_args()

api_key = os.environ.get("GEMINI_API_KEY", "").strip()
if not api_key:
    print("GEMINI_API_KEY is not set.", file=sys.stderr)
    sys.exit(1)

client = OpenAI(
    api_key=api_key,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

docs = []
for raw_path in args.paths:
    path = pathlib.Path(raw_path)
    if path.exists():
        try:
            content = path.read_text(errors="ignore")
        except Exception as exc:
            content = f"Could not read file: {exc}"
    else:
        content = "File not found."
    docs.append(f"<file path='{raw_path}'>\n{content}\n</file>")

try:
    response = client.chat.completions.create(
        model=os.environ.get("GEMINI_READ_MODEL", "gemini-2.5-flash"),
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert infrastructure and systems code analyst. "
                    "Analyze the provided file corpus and answer concisely with actionable findings."
                ),
            },
            {"role": "user", "content": "<corpus>\n" + "\n\n".join(docs) + "\n</corpus>"},
            {"role": "user", "content": args.question},
        ],
        max_tokens=int(os.environ.get("GEMINI_READ_MAX_TOKENS", "8192")),
    )
    print(response.choices[0].message.content)
except Exception as exc:
    print(f"Gemini API error: {exc}", file=sys.stderr)
    sys.exit(1)
PY

log "Writing gemini-write..."
cat > "${APP_DIR}/gemini_write.py" <<'PY'
#!/usr/bin/env python3
import argparse
import os
import pathlib
import sys

from openai import OpenAI


parser = argparse.ArgumentParser(description="Delegate boilerplate/config writing to Gemini.")
parser.add_argument("--spec", required=True, help="What to generate")
parser.add_argument("--context", required=True, help="Reference file, or 'none'")
parser.add_argument("--target", required=True, help="Output file")
args = parser.parse_args()

api_key = os.environ.get("GEMINI_API_KEY", "").strip()
if not api_key:
    print("GEMINI_API_KEY is not set.", file=sys.stderr)
    sys.exit(1)

if args.context.lower() == "none":
    context = "No reference context was provided."
else:
    context_path = pathlib.Path(args.context)
    context = context_path.read_text(errors="ignore") if context_path.exists() else "Reference file not found."

client = OpenAI(
    api_key=api_key,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

try:
    response = client.chat.completions.create(
        model=os.environ.get("GEMINI_WRITE_MODEL", "gemini-2.5-flash"),
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an advanced software builder. Output only the raw file content. "
                    "Do not include markdown fences, explanations, or chatter."
                ),
            },
            {"role": "user", "content": f"<reference_context>\n{context}\n</reference_context>"},
            {"role": "user", "content": f"Generate this file content: {args.spec}"},
        ],
        max_tokens=int(os.environ.get("GEMINI_WRITE_MAX_TOKENS", "16384")),
    )
    output = response.choices[0].message.content.strip()
    pathlib.Path(args.target).write_text(output)
    print(f"Success: content written to {args.target}")
except Exception as exc:
    print(f"Gemini API error: {exc}", file=sys.stderr)
    sys.exit(1)
PY

chmod 0644 "${APP_DIR}/ask_gemini.py" "${APP_DIR}/gemini_write.py"

log "Writing user command wrappers..."
cat > "${BIN_DIR}/ask-gemini" <<EOF
#!/usr/bin/env sh
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"
exec "${VENV_DIR}/bin/python" "${APP_DIR}/ask_gemini.py" "\$@"
EOF
cat > "${BIN_DIR}/gemini-write" <<EOF
#!/usr/bin/env sh
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"
exec "${VENV_DIR}/bin/python" "${APP_DIR}/gemini_write.py" "\$@"
EOF
chmod 0755 "${BIN_DIR}/ask-gemini" "${BIN_DIR}/gemini-write"

log "Writing system wrappers when possible..."
if [ -w /usr/local/bin ] || [ "$(id -u)" = "0" ]; then
  cat > /usr/local/bin/ask-gemini <<EOF
#!/usr/bin/env sh
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"
exec "${VENV_DIR}/bin/python" "${APP_DIR}/ask_gemini.py" "\$@"
EOF
  cat > /usr/local/bin/gemini-write <<EOF
#!/usr/bin/env sh
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"
exec "${VENV_DIR}/bin/python" "${APP_DIR}/gemini_write.py" "\$@"
EOF
  chmod 0755 /usr/local/bin/ask-gemini /usr/local/bin/gemini-write
else
  warn "Cannot write /usr/local/bin wrappers. Use ${BIN_DIR}/ask-gemini and ${BIN_DIR}/gemini-write directly."
fi

if [ -n "${GEMINI_API_KEY:-}" ]; then
  log "Persisting GEMINI_API_KEY from current environment to ${ENV_FILE}..."
  {
    printf 'export GEMINI_API_KEY='
    shell_quote "${GEMINI_API_KEY}"
    printf '\n'
  } > "${ENV_FILE}"
  chmod 0600 "${ENV_FILE}"
elif [ "${PROMPT_FOR_KEY}" = "1" ] && [ -t 0 ]; then
  printf 'Enter GEMINI_API_KEY for this server (input hidden, blank to skip): '
  stty -echo
  read -r entered_key || true
  stty echo
  printf '\n'
  if [ -n "${entered_key}" ]; then
    {
      printf 'export GEMINI_API_KEY='
      shell_quote "${entered_key}"
      printf '\n'
    } > "${ENV_FILE}"
    chmod 0600 "${ENV_FILE}"
    export GEMINI_API_KEY="${entered_key}"
    log "Saved GEMINI_API_KEY to ${ENV_FILE}."
  else
    warn "GEMINI_API_KEY was not saved. Add it later to ${ENV_FILE}."
  fi
else
  warn "GEMINI_API_KEY is not set. Add it later to ${ENV_FILE} or export it before using the tools."
fi

log "Writing ${AGENTS_FILE} routing instructions..."
cat > "${AGENTS_FILE}" <<'AGENTS'
### Core Rules for Routing
- ChatGPT/Codex = Architecture, structural troubleshooting, complex error parsing, permissions fixes, and network logic.
- Gemini via tools = Bulk data reading, log ingestion, raw text generation, and boilerplate writing.
- MANDATORY: Do not read more than two docker/config/log files simultaneously. Run the `ask-gemini` shell command and act on the text summary it returns.

### Available Gemini Commands
- Bulk reader:
  `ask-gemini --paths <file1> <file2> --question "<what you need to know>"`
- Boilerplate writer:
  `gemini-write --spec "<what to generate>" --context <reference-file-or-none> --target <output-file>`
AGENTS

if ! grep -Fq ".gemini-worker.env" "${HOME_DIR}/.bashrc" 2>/dev/null; then
  log "Updating ${HOME_DIR}/.bashrc with PATH and GEMINI_API_KEY loader..."
  cat >> "${HOME_DIR}/.bashrc" <<EOF

# Danish Intelligence AI debug tools
export PATH="\$HOME/bin:\$PATH"
[ -f "\$HOME/.gemini-worker.env" ] && . "\$HOME/.gemini-worker.env"
EOF
fi

if [ "${INSTALL_CODEX}" = "1" ]; then
  if need_cmd codex; then
    log "Codex CLI already installed: $(command -v codex)"
  elif need_cmd npm; then
    log "Installing Codex CLI with npm..."
    if npm install -g @openai/codex; then
      log "Codex CLI installed."
    else
      warn "Codex CLI install failed. You can install it later with: npm install -g @openai/codex"
    fi
  else
    warn "npm is unavailable. Install Codex later after installing Node/npm."
  fi
fi

log "Running offline checks..."
"${VENV_DIR}/bin/python" -c "import openai; print('openai python package OK')"
command -v ask-gemini >/dev/null 2>&1 && log "ask-gemini wrapper OK: $(command -v ask-gemini)" || warn "ask-gemini is not on PATH yet. Open a new shell or source ~/.bashrc."
command -v gemini-write >/dev/null 2>&1 && log "gemini-write wrapper OK: $(command -v gemini-write)" || warn "gemini-write is not on PATH yet. Open a new shell or source ~/.bashrc."

if [ "${RUN_API_TESTS}" = "1" ]; then
  if [ -f "${ENV_FILE}" ]; then
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
  fi
  if [ -n "${GEMINI_API_KEY:-}" ]; then
    tmp_context="$(mktemp)"
    tmp_output="$(mktemp)"
    printf 'Gemini worker smoke test context.\n' > "${tmp_context}"
    log "Running live ask-gemini smoke test..."
    ask-gemini --paths "${tmp_context}" --question "Reply with exactly: ask-gemini OK"
    log "Running live gemini-write smoke test..."
    gemini-write --spec "Write exactly: gemini-write OK" --context none --target "${tmp_output}"
    cat "${tmp_output}"
    printf '\n'
    rm -f "${tmp_context}" "${tmp_output}"
  else
    warn "Skipping live API tests because GEMINI_API_KEY is not set."
  fi
fi

cat <<EOF

AI debug tools installed.

Next steps on the other server:
  1. Open a new shell, or run: source ~/.bashrc
  2. If you did not pass GEMINI_API_KEY, add it to: ${ENV_FILE}
  3. Start Codex from ${HOME_DIR} so it reads: ${AGENTS_FILE}
  4. Quick status commands:
       ask-gemini --paths ${AGENTS_FILE} --question "What is the division of labor?"
       gemini-write --spec "Write a one-line test file saying OK" --context none --target /tmp/gemini-write-test.txt

EOF
