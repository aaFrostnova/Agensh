# Organization bootstrap

One-shot provisioning container for the team's Gitea and optional Mattermost
services. It runs after service health checks and Gitea administrator creation.

| File | Purpose |
|---|---|
| `bootstrap.sh` | Provisions identities, permissions, repositories and cached credentials |
| `Dockerfile` | Alpine image with Bash, curl, jq, Git and OpenSSL |

`AGENTS` and `REPOS` are comma-separated lists supplied by generated Compose.
The script creates the Gitea organization and its `coders` team, synchronizes
passwords, and configures `main` so coders merge PRs with zero required approvals
while direct pushes are restricted to the administrator. It also installs
CODEOWNERS, disables worker auto-watching and obtains an Actions runner token.

Gitea passwords/API tokens and Mattermost session tokens are cached in
`$BOOTSTRAP_DATA_DIR/tokens` with private permissions. Valid tokens are reused;
stale tokens are rotated. The router reads Gitea passwords for SSE form login,
and the [token service](../../secret-service/README.md) serves cached API tokens.

`.bootstrap-done` skips completed provisioning on later starts.
`PBENCH_BOOTSTRAP_FORCE=1` reruns provisioning when set inside the bootstrap
container; generated Compose does not forward that variable from the host.
`make bootstrap-rerun` removes the marker for the selected profile. Task seeding
and the private `pbench-task` channel are handled later by
[`pbench_run.sh`](../../scripts/pbench_run.sh).
