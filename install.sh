#!/bin/sh
# RAVE installer for Mac, Ubuntu, and Linux servers (VPS).
# Run:  curl -fsSL https://raw.githubusercontent.com/PrabakaranR-code/rave/main/install.sh | sh
set -e

REPO_URL="${RAVE_REPO_URL:-https://github.com/PrabakaranR-code/rave}"
DIR="${RAVE_HOME:-$HOME/rave}"
BIN_DIR="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# When piped from curl, stdin is the script itself — talk to the person
# through the terminal device instead.
wait_enter() {
  if [ -r /dev/tty ]; then
    printf '%s' "(press Enter when done) " > /dev/tty
    IFS= read -r _line < /dev/tty || true
  else
    say "No keyboard available — continuing."
  fi
}

open_page() {
  if have open; then open "$1" || true
  elif have xdg-open; then xdg-open "$1" >/dev/null 2>&1 || true
  else say "Open this page in your browser: $1"; fi
}

OS="linux"
case "$(uname -s)" in
  Darwin) OS="mac" ;;
  Linux)
    if grep -qiE 'ubuntu|debian' /etc/os-release 2>/dev/null; then OS="ubuntu"; fi ;;
esac
say "→ Installing RAVE on $OS"

APT="apt-get"
[ "$(id -u)" != "0" ] && have sudo && APT="sudo apt-get"

# --- Python 3.11+ (the language RAVE runs on) -------------------------------
find_python() {
  for c in python3.13 python3.12 python3.11 python3; do
    if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"
while [ -z "$PY" ]; do
  if [ "$OS" = "ubuntu" ]; then
    say "→ Installing Python (the language RAVE runs on)…"
    $APT update -y >/dev/null 2>&1 || true
    $APT install -y python3.12 python3.12-venv 2>/dev/null \
      || $APT install -y python3.11 python3.11-venv 2>/dev/null \
      || $APT install -y python3 python3-venv python3-pip || true
    PY="$(find_python || true)"
    [ -z "$PY" ] && { say "✗ Could not install Python 3.11+. Install it, then re-run."; exit 1; }
  else
    say "RAVE needs Python 3.11 or newer (the language RAVE runs on)."
    say "I've opened the download page. Install it, then come back."
    open_page "https://www.python.org/downloads/"
    wait_enter
    PY="$(find_python || true)"
  fi
done
say "✓ Python found: $PY"

# --- Git (a program that downloads code) ------------------------------------
while ! have git; do
  if [ "$OS" = "ubuntu" ]; then
    say "→ Installing Git (a program that downloads code)…"
    $APT update -y >/dev/null 2>&1 || true
    $APT install -y git || { say "✗ Could not install Git."; exit 1; }
  else
    say "RAVE needs Git (a program that downloads code)."
    say "I've opened the download page. Install it, then come back."
    open_page "https://git-scm.com/downloads"
    wait_enter
  fi
done
say "✓ Git found"

# --- Docker (a helper program for the search engine) — optional here --------
if ! have docker && [ "$OS" = "ubuntu" ]; then
  say "→ Installing Docker (a helper program for the search engine)…"
  $APT install -y docker.io >/dev/null 2>&1 || true
fi

# --- Get the code, build the sandbox, install dependencies ------------------
if [ -d "$DIR/.git" ]; then
  say "→ Updating RAVE in $DIR"
  git -C "$DIR" pull --ff-only || true
else
  say "→ Downloading RAVE to $DIR"
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"
"$PY" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
say "✓ RAVE installed"

# --- The `rave` command ------------------------------------------------------
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/rave" <<EOF
#!/bin/sh
exec "$DIR/.venv/bin/python" "$DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/rave"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Note: add $BIN_DIR to your PATH to use the rave command everywhere." ;;
esac
say "✓ Command installed: rave"

# --- Hand over to the setup wizard ------------------------------------------
if [ -r /dev/tty ]; then
  exec "$DIR/.venv/bin/python" "$DIR/main.py" setup < /dev/tty
else
  say "Setup is next — run:  rave setup"
fi
