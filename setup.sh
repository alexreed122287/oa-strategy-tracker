#!/usr/bin/env bash
# =============================================================================
# One-shot deploy for the Option Alpha Strategy Tracker.
# Run this INSIDE your GitHub Codespace (gh CLI is pre-authenticated there),
# from the folder containing these project files:
#
#   bash setup.sh [repo-name]        # default repo name: oa-strategy-tracker
#
# It will: create the repo, commit & push everything, grant the workflow
# write permission, enable GitHub Pages, prompt YOU for your Tradier API
# token (stored directly as an encrypted repo secret — it never appears in
# shell history or logs), and kick off the first market-close update.
# =============================================================================
set -euo pipefail

REPO_NAME="${1:-oa-strategy-tracker}"

command -v gh >/dev/null || { echo "gh CLI not found — run this in a Codespace or install https://cli.github.com"; exit 1; }
gh auth status >/dev/null || { echo "gh is not authenticated. Run: gh auth login"; exit 1; }
OWNER=$(gh api user -q .login)
echo "==> Deploying as $OWNER/$REPO_NAME"

# --- git init, commit, create repo, push -------------------------------------
if [ ! -d .git ]; then
  git init -b main
fi
git add -A
git commit -m "Option Alpha strategy tracker — initial deploy" || echo "(nothing new to commit)"

if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  echo "==> Repo exists; pushing to it."
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  git push -u origin main
else
  gh repo create "$REPO_NAME" --public --source=. --remote=origin --push
fi

# --- allow the Action to commit data back ------------------------------------
echo "==> Granting workflow write permissions"
gh api -X PUT "repos/$OWNER/$REPO_NAME/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false >/dev/null

# --- enable GitHub Pages (main / root) ----------------------------------------
echo "==> Enabling GitHub Pages"
gh api -X POST "repos/$OWNER/$REPO_NAME/pages" \
  -f "source[branch]=main" -f "source[path]=/" >/dev/null 2>&1 \
  || echo "    (Pages may already be enabled — fine)"

# --- Tradier token as an encrypted repo secret --------------------------------
echo
echo "==> Tradier API token (from https://dash.tradier.com → API Access)."
echo "    You'll paste it into gh directly; it is encrypted by GitHub and"
echo "    never written to disk or history. Press Enter to skip and use the"
echo "    free Yahoo Finance fallback instead."
if gh secret set TRADIER_TOKEN --repo "$OWNER/$REPO_NAME"; then
  echo "    Secret TRADIER_TOKEN set."
else
  echo "    Skipped — tracker will use Yahoo Finance until you add the secret:"
  echo "    gh secret set TRADIER_TOKEN --repo $OWNER/$REPO_NAME"
fi

# --- first run -----------------------------------------------------------------
echo "==> Triggering first market-close update"
sleep 3
gh workflow run update.yml --repo "$OWNER/$REPO_NAME" \
  && echo "    Workflow dispatched — watch it: gh run watch --repo $OWNER/$REPO_NAME" \
  || echo "    Could not dispatch yet (workflows index for ~1 min after first push). Re-run: gh workflow run update.yml"

echo
echo "=============================================================="
echo " Dashboard will be live at:"
echo "   https://$OWNER.github.io/$REPO_NAME/"
echo " (first Pages build takes a minute or two)"
echo "=============================================================="
