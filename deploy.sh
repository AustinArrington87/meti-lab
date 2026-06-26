#!/usr/bin/env bash
# Deploy local changes to the meti-lab EC2 (lab.millpont.com).
# Usage: ./deploy.sh
# Optionally pass specific files: ./deploy.sh backend/services/claude_agent.py

set -e

EC2_HOST="ubuntu@44.204.209.189"
KEY="$HOME/.ssh/meti-lab.pem"
REMOTE_DIR="/home/ubuntu/meti-lab"
SSH_OPTS="-o StrictHostKeyChecking=no -i $KEY"

# Determine files to deploy
if [ "$#" -gt 0 ]; then
  FILES=("$@")
  echo "Deploying ${#FILES[@]} file(s)..."
else
  # Default: deploy all tracked changes (modified + untracked non-.env)
  FILES=($(git diff --name-only HEAD) $(git ls-files --others --exclude-standard | grep -v '\.env'))
  if [ "${#FILES[@]}" -eq 0 ]; then
    echo "No changed files to deploy. Restarting services..."
  else
    echo "Deploying ${#FILES[@]} changed file(s)..."
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

echo "Done — https://lab.millpont.com"
