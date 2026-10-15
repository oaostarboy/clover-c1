#!/usr/bin/env bash
# Native disposable-runner adaptation of the historical POSIX/macOS proofs.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]]
MODE=$1
PROOF="$GITHUB_WORKSPACE/helpers/.github/native-proof/proof.py"
SOURCE="$GITHUB_WORKSPACE/source"
EVIDENCE="$RUNNER_TEMP/evidence"
mkdir -p "$EVIDENCE" "$RUNNER_TEMP/neutral"
python "$PROOF" preflight --repo "$SOURCE" --output "$EVIDENCE/plan.json"
unset PYTHONPATH VIRTUAL_ENV
export CLOVER_HOME="$HOME/.clover" CLOVER_INSTALL_DIR="$HOME/.clover/clover-c1"
INSTALL_DIR="$CLOVER_INSTALL_DIR"
PY="$INSTALL_DIR/venv/bin/python"
BIN="$INSTALL_DIR/venv/bin/clover"
# A fresh runner is mandatory. Never adopt or replace an existing install.
test ! -e "$CLOVER_HOME"
if command -v clover; then exit 1; fi
FROM_SHA=$TARGET_REF
TO_SHA=$TARGET_REF
TO_TREE=$TARGET_TREE
if [[ $MODE == old2new ]]; then FROM_SHA=$OLD_SHA; fi
if [[ $MODE == new2next ]]; then TO_SHA=$NEXT_SHA; TO_TREE=$NEXT_TREE; fi
git -C "$SOURCE" show "$TARGET_REF:scripts/install.sh" > "$RUNNER_TEMP/install.sh"
bash "$RUNNER_TEMP/install.sh" --branch "$NEXT_REF" --commit "$FROM_SHA" \
  --non-interactive --skip-setup --skip-browser --skip-computer-use 2>&1 | tee "$EVIDENCE/install.log"
cd "$RUNNER_TEMP/neutral"
FROM_TREE=$(git -C "$SOURCE" rev-parse "$FROM_SHA^{tree}")
"$PY" "$PROOF" identity --repo "$INSTALL_DIR" --sha "$FROM_SHA" --tree "$FROM_TREE" --output "$EVIDENCE/before-imports.json"
if [[ $MODE != fresh ]]; then
  # The updater's default main path sees a local bare origin with real objects.
  # Keep this fixture topology explicit in the evidence; it is NOT GitHub main.
  git -C "$INSTALL_DIR" fetch --no-tags "$SOURCE" "$TO_SHA"
  "$PY" "$PROOF" origin --repo "$INSTALL_DIR" --mirror "$RUNNER_TEMP/origin.git" --target "$TO_SHA"
fi
printf '\nAPI_SERVER_ENABLED=true\nAPI_SERVER_KEY=native-proof-dummy-key-123456\nAPI_SERVER_PORT=18791\nAPI_SERVER_HOST=127.0.0.1\n' >> "$CLOVER_HOME/.env"
"$BIN" gateway run > "$EVIDENCE/gateway-launch.log" 2>&1 &
"$PY" "$PROOF" gateway --repo "$INSTALL_DIR" --home "$CLOVER_HOME" --sha "$FROM_SHA" --port 18791 --seconds 0 --output "$EVIDENCE/before-state.json"
if [[ $MODE != fresh ]]; then
  "$PY" "$PROOF" begin --output "$EVIDENCE/begin.json"
  # This is gateway-mode CLI, not a claimed chat/Telegram transport test.
  "$BIN" update --yes --gateway 2>&1 | tee "$EVIDENCE/update.log"
  "$PY" "$PROOF" gateway --repo "$INSTALL_DIR" --home "$CLOVER_HOME" --sha "$TO_SHA" --port 18791 --seconds 90 --previous "$EVIDENCE/before-state.json" --output "$EVIDENCE/after-state.json"
  "$PY" "$PROOF" receipt --home "$CLOVER_HOME" --begin "$EVIDENCE/begin.json" --old "$FROM_SHA" --target "$TO_SHA" --output "$EVIDENCE/update-receipt.json"
else
  "$PY" "$PROOF" gateway --repo "$INSTALL_DIR" --home "$CLOVER_HOME" --sha "$TO_SHA" --port 18791 --seconds 90 --output "$EVIDENCE/fresh-state.json"
fi
"$PY" "$PROOF" identity --repo "$INSTALL_DIR" --sha "$TO_SHA" --tree "$TO_TREE" --output "$EVIDENCE/after-imports.json"
# Native feature checks execute separately, after the actual installed-source
# identity probe, in the installed interpreter. Existing guards remain active.
if [[ $MODE == fresh ]]; then
  "$PY" "$GITHUB_WORKSPACE/helpers/.github/native-proof/native_tests.py" --repo "$INSTALL_DIR" --evidence "$EVIDENCE"
fi
