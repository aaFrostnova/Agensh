#!/usr/bin/env bash
# One-time, idempotent setup of the rootless backend (no Docker, no sudo) under $PROJECT_ROOT.
# Needs: apptainer, bwrap, curl, git, uv, network access on this node.
set -euo pipefail

: "${PROJECT_ROOT:?set PROJECT_ROOT to the /scratch project directory (plan 0.1)}"
REPO="$PROJECT_ROOT/repo/Agensh"
TOOLS="$PROJECT_ROOT/tools"
IMAGES="$PROJECT_ROOT/images"
NODE_VERSION=v22.14.0          # pinned like agent/Dockerfile
COPILOT_VERSION=1.0.70         # pinned like agent/Dockerfile
MATTERMOST_MCP_VERSION=0.5.1   # pinned like agent/Dockerfile
mkdir -p "$TOOLS/bin" "$IMAGES" "$PROJECT_ROOT"/{cache,tmp,logs,results,runs,data,secrets}
chmod 700 "$PROJECT_ROOT/secrets"
export UV_CACHE_DIR="$PROJECT_ROOT/cache/uv" UV_PYTHON_INSTALL_DIR="$TOOLS/uv-python" UV_TOOL_DIR="$TOOLS/uv-tools" \
       UV_TOOL_BIN_DIR="$TOOLS/bin" APPTAINER_CACHEDIR="$PROJECT_ROOT/cache/apptainer" APPTAINER_TMPDIR="$PROJECT_ROOT/tmp" \
       npm_config_cache="$PROJECT_ROOT/cache/npm" npm_config_update_notifier=false

echo "== Node $NODE_VERSION (sha256-verified)"
NODE_DIR="$TOOLS/node-$NODE_VERSION-linux-x64"
if [ ! -x "$NODE_DIR/bin/node" ]; then
    tarball="node-$NODE_VERSION-linux-x64.tar.xz"
    curl -fsSL -o "$PROJECT_ROOT/tmp/$tarball" "https://nodejs.org/dist/$NODE_VERSION/$tarball"
    curl -fsSL -o "$PROJECT_ROOT/tmp/SHASUMS256.txt" "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt"
    want="$(awk -v f="$tarball" '$2==f{print $1}' "$PROJECT_ROOT/tmp/SHASUMS256.txt")"
    [ "$(sha256sum "$PROJECT_ROOT/tmp/$tarball" | cut -d' ' -f1)" = "$want" ] || { echo "node checksum mismatch" >&2; exit 1; }
    tar -xJf "$PROJECT_ROOT/tmp/$tarball" -C "$TOOLS"
fi
export PATH="$NODE_DIR/bin:$PATH"

echo "== Copilot CLI $COPILOT_VERSION"
if [ ! -x "$TOOLS/copilot-cli/node_modules/.bin/copilot" ]; then
    mkdir -p "$TOOLS/copilot-cli"
    (cd "$TOOLS/copilot-cli" && npm init -y >/dev/null && npm install --omit=dev --no-audit --no-fund --save-exact "@github/copilot@$COPILOT_VERSION")
fi
cat > "$TOOLS/bin/copilot-wrapped" <<EOF
#!/bin/sh
exec $NODE_DIR/bin/node $TOOLS/copilot-cli/node_modules/@github/copilot/npm-loader.js "\$@"
EOF
chmod +x "$TOOLS/bin/copilot-wrapped"

echo "== mini-router build"
(cd "$REPO/mini-router" && npm ci --no-audit --no-fund && npx tsc)

echo "== Python environments"
[ -d "$TOOLS/venv-board" ] || uv venv --python 3.11 "$TOOLS/venv-board"
uv pip install --python "$TOOLS/venv-board/bin/python" -r "$REPO/board-service/requirements.txt"
[ -d "$TOOLS/venv-exp" ] || uv venv --python 3.11 "$TOOLS/venv-exp"
uv pip install --python "$TOOLS/venv-exp/bin/python" pytest pyyaml jsonschema requests numpy pandas scipy statsmodels pyarrow
uv tool install --python 3.11 "mcp-server-mattermost==$MATTERMOST_MCP_VERSION"

echo "== service images (Apptainer)"
pull() {
    if [ ! -s "$IMAGES/$1" ]; then
        apptainer pull --disable-cache "$IMAGES/$1.partial" "$2"
        mv -f "$IMAGES/$1.partial" "$IMAGES/$1"
    fi
}
pull mysql_8.4.sif docker://mysql:8.4
pull gitea_1.22.6.sif docker://docker.gitea.com/gitea:1.22.6
pull mattermost-team-edition_10.6.sif docker://mattermost/mattermost-team-edition:10.6
pull gitea-mcp-server_latest.sif docker://docker.gitea.com/gitea-mcp-server:latest

echo "== gitea-mcp binary (the image has no shell, so unpack it)"
if [ ! -s "$TOOLS/bin/gitea-mcp" ]; then
    sandbox="$PROJECT_ROOT/tmp/gmcp_sandbox"
    rm -rf "$sandbox"
    apptainer build --sandbox --fix-perms "$sandbox" "$IMAGES/gitea-mcp-server_latest.sif"
    cp "$sandbox/app/gitea-mcp" "$TOOLS/bin/gitea-mcp" && chmod +x "$TOOLS/bin/gitea-mcp"
    rm -rf "$sandbox"
fi

echo "== checks"
"$NODE_DIR/bin/node" --version
"$TOOLS/bin/copilot-wrapped" --version | head -1
"$TOOLS/bin/gitea-mcp" --help | head -1
[ -f "$PROJECT_ROOT/secrets/qwen_local.env" ] || { umask 077; printf 'QWEN_LOCAL_API_KEY=%s\n' "$(openssl rand -hex 24)" > "$PROJECT_ROOT/secrets/qwen_local.env"; }
echo "setup complete. Bedrock key (for Kimi K3): put BEDROCK_API_KEY=... in $PROJECT_ROOT/secrets/bedrock.env (mode 600)."
