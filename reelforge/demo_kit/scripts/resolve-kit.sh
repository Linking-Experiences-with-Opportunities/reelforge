#!/usr/bin/env bash
# resolve-kit.sh — print the absolute path of the demo-kit directory, or exit 1.
# ${CLAUDE_PLUGIN_ROOT} is NOT set in Bash, so probe in order:
probe() { [ -f "$1/kit.json" ] && { (cd "$1" && pwd -P); exit 0; }; }
[ -n "${DEMO_KIT_DIR:-}" ] && probe "$DEMO_KIT_DIR"
[ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && probe "$CLAUDE_PLUGIN_ROOT/demo-kit"
newest="$(ls -1d "$HOME"/.claude/plugins/cache/record-demo-kit/record-demo/*/demo-kit 2>/dev/null | sort -V | tail -1)"
[ -n "$newest" ] && probe "$newest"
probe "$HOME/.claude/plugins/marketplaces/record-demo-kit/plugins/record-demo/demo-kit"
# a local clone of the repo
root="$(git rev-parse --show-toplevel 2>/dev/null)" && [ -n "$root" ] && probe "$root/plugins/record-demo/demo-kit"
exit 1
