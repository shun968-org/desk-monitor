#!/usr/bin/env bash
# Makes every `claude` invocation inside the devcontainer implicitly pass
# --settings <repo file>, so that sandbox.network.strictAllowlist (which
# Claude Code only honors from user/managed/CLI settings, never from a
# committed .claude/settings.json — see CLAUDE.md's "Dev container" section)
# actually takes effect for everyone using this devcontainer, without each
# developer having to edit their own ~/.claude/settings.json.
set -euo pipefail

STRICT_SETTINGS=/workspace/.devcontainer/claude-strict-network-settings.json
CLAUDE_BIN="$(command -v claude || true)"

if [ -z "$CLAUDE_BIN" ]; then
  echo "wrap-claude-cli.sh: claude not found on PATH, skipping" >&2
  exit 0
fi

# Idempotent: skip if already wrapped (e.g. postCreateCommand re-run).
if [ -f "${CLAUDE_BIN}.real" ]; then
  exit 0
fi

mv "$CLAUDE_BIN" "${CLAUDE_BIN}.real"
cat > "$CLAUDE_BIN" <<WRAPPER
#!/usr/bin/env bash
exec "${CLAUDE_BIN}.real" --settings "$STRICT_SETTINGS" "\$@"
WRAPPER
chmod +x "$CLAUDE_BIN"
