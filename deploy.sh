#!/usr/bin/env bash
# Deploy to the meti-lab EC2 (lab.millpont.com).
#
# Usage:
#   ./deploy.sh                    # deploy all commits since last deploy
#   ./deploy.sh file1 file2 ...    # deploy specific files
#
# Tracks the last deployed commit in .deployed_commit so it can diff against
# the current HEAD and deploy only what changed between commits.

set -e

EC2_HOST="ubuntu@44.204.209.189"
KEY="$HOME/.ssh/meti-lab.pem"
REMOTE_DIR="/home/ubuntu/meti-lab"
SSH_OPTS="-o StrictHostKeyChecking=no -i $KEY"
TRACKING_FILE=".deployed_commit"

# Determine files to deploy
if [ "$#" -gt 0 ]; then
  FILES=("$@")
  echo "Deploying ${#FILES[@]} specified file(s)..."
else
  CURRENT=$(git rev-parse HEAD)

  if [ -f "$TRACKING_FILE" ]; then
    LAST=$(cat "$TRACKING_FILE")
    if [ "$LAST" = "$CURRENT" ]; then
      echo "Already deployed at $CURRENT — nothing new to deploy."
      echo "Run: ./deploy.sh file1 file2 ... to deploy specific files."
      exit 0
    fi
    echo "Deploying changes since $LAST..."
    mapfile -t FILES < <(git diff --name-only "$LAST" "$CURRENT" | grep -v '\.env' || true)
    # Also include any uncommitted working-tree changes
    mapfile -t DIRTY < <(git diff --name-only HEAD | grep -v '\.env' || true)
    FILES=("${FILES[@]}" "${DIRTY[@]}")
  else
    echo "No prior deploy tracked — deploying all uncommitted changes..."
    mapfile -t FILES < <(git diff --name-only HEAD | grep -v '\.env' || true)
    mapfile -t UNTRACKED < <(git ls-files --others --exclude-standard | grep -v '\.env' || true)
    FILES=("${FILES[@]}" "${UNTRACKED[@]}")
  fi

  if [ "${#FILES[@]}" -eq 0 ]; then
    echo "No changed files detected — restarting services only."
  else
    echo "Deploying ${#FILES[@]} file(s)..."
  fi
fi

# SCP each file
for f in "${FILES[@]}"; do
  if [ -f "$f" ]; then
    REMOTE_PATH="$REMOTE_DIR/$f"
    REMOTE_PARENT=$(dirname "$REMOTE_PATH")
    ssh $SSH_OPTS "$EC2_HOST" "mkdir -p $REMOTE_PARENT"
    scp $SSH_OPTS "$f" "$EC2_HOST:$REMOTE_PATH"
    echo "  ✓ $f"
  fi
done

# Restart services
echo "Restarting services..."
ssh $SSH_OPTS "$EC2_HOST" \
  "sudo systemctl restart meti-backend meti-flask && sleep 2 && sudo systemctl is-active meti-backend meti-flask"

# Record deployed commit (only when auto-detecting, not when passing specific files)
if [ "$#" -eq 0 ]; then
  git rev-parse HEAD > "$TRACKING_FILE"
fi

echo "Done — https://lab.millpont.com"
