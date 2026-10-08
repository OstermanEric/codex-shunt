#!/bin/bash
# One-command install for macOS, Linux, and WSL2. No sudo or automatic hook trust.

main() {
  set -euo pipefail
  local checkout="$HOME/.codex/plugins/codex-shunt"
  local repository="https://github.com/OstermanEric/codex-shunt.git"
  local codex_cli profile
  local shell_name="${SHELL:-}"

  command -v git >/dev/null || { echo "Install Git, then run this installer again." >&2; return 1; }
  command -v python3 >/dev/null || { echo "Install Python 3.11+, then run this installer again." >&2; return 1; }
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required.")'

  if [ -e "$checkout" ]; then
    [ -d "$checkout/.git" ] || { echo "Refusing to replace existing folder: $checkout" >&2; return 1; }
    local remote
    remote=$(git -C "$checkout" remote get-url origin)
    [ "${remote%.git}" = "${repository%.git}" ] || { echo "Refusing to update an unrelated repository: $checkout" >&2; return 1; }
    [ -z "$(git -C "$checkout" status --porcelain)" ] || { echo "Save your local changes in $checkout before updating." >&2; return 1; }
    git -C "$checkout" pull --ff-only
  else
    mkdir -p "$(dirname "$checkout")"
    git clone --depth 1 "$repository" "$checkout"
  fi

  codex_cli=$(python3 - "$checkout/src" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from codex_shunt.worker import find_codex
print(find_codex())
PY
  )
  "$codex_cli" plugin marketplace add "$checkout"
  "$codex_cli" plugin add codex-shunt@codex-shunt
  "$checkout/scripts/install-command" >/dev/null
  echo "Installed the shunt command."

  # Make the command available in future standard bash/zsh Terminal sessions.
  if [ "${CODEX_SHUNT_BIN_DIR:-$HOME/.local/bin}" = "$HOME/.local/bin" ] &&
     [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    case "${shell_name##*/}" in
      zsh) profile="${ZDOTDIR:-$HOME}/.zshrc" ;;
      bash)
        profile="$HOME/.bashrc"
        if [[ "$OSTYPE" == darwin* ]]; then
          profile="$HOME/.bash_profile"
          for candidate in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
            if [ -f "$candidate" ]; then profile="$candidate"; break; fi
          done
        fi
        ;;
      *) profile="" ;;
    esac
    if [ -n "$profile" ]; then
      python3 - "$profile" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
marker = '# Codex Shunt command'
existing = path.read_text() if path.exists() else ''
if marker not in existing:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        handle.write('\n' + marker + '\nexport PATH="$HOME/.local/bin:$PATH"\n')
PY
      echo "Added shunt to PATH in $profile. Reopen Terminal to use it."
    fi
  fi

  echo
  echo "Shunt is installed. Starting guided setup..."
  if [ -t 0 ]; then
    "$checkout/scripts/codex-shunt" setup
  elif ( : </dev/tty ) 2>/dev/null; then
    "$checkout/scripts/codex-shunt" setup </dev/tty
  else
    echo "Open a local Codex chat and run \$codex-shunt:setup to complete setup."
    return 0
  fi

  echo
  echo "Review and trust Codex Shunt's hook in Codex, then start a new chat."
  echo "View usage with: shunt stats --since 7d"
}

main "$@"
