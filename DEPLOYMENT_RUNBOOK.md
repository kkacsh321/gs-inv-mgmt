# GoldenStackers Deployment Runbook

This runbook defines release and promotion flow for contained Kubernetes environments managed by ArgoCD from a separate infra repo.

## 1) Release Artifact Contract

- Release image tags: `vX.Y.Z`
- Traceability image tags: `sha-<full_git_sha>`
- Main pre-release tags: `main`, `main-<short_sha>`, `sha-<full_git_sha>`
- Promotion rule: promote the same immutable image tag from Dev to Prod (do not rebuild per environment).

## 2) Required Repos

- App repo (this repo): builds/tests/publishes images via GitHub Actions.
- Infra repo (ArgoCD-managed): stores Kubernetes manifests and environment overlays.

## 3) Required GitHub Configuration

Secrets:
- `DOCKER_REGISTRY`
- `DOCKER_USERNAME`
- `DOCKER_PASSWORD`
- optional `GH_TOKEN` for cross-repo PRs

Repository variables (app repo):
- `ARGO_DEPLOY_REPO`
- `ARGO_DEV_MANIFEST_PATH`
- optional `ARGO_DEV_CONFIGMAP_PATH` (to auto-write `APP_BUILD_VERSION` + `APP_BUILD_SHA` in Dev)
- `ARGO_PROD_MANIFEST_PATH`
- optional `ARGO_PROD_CONFIGMAP_PATH` (to auto-write `APP_BUILD_VERSION` + `APP_BUILD_SHA` in Prod)
- optional `ARGO_TARGET_BRANCH` (default `main`)
- optional `ARGO_PR_LABELS` (default `automerge`)
- optional `ARGO_DEV_PR_LABELS`
- optional `ARGO_PROD_PR_LABELS`

Example values:

```text
ARGO_DEPLOY_REPO=your-org/your-argo-repo
ARGO_TARGET_BRANCH=main
ARGO_DEV_MANIFEST_PATH=apps/gs-inv/dev/deployment-app.yaml
ARGO_DEV_CONFIGMAP_PATH=apps/gs-inv/dev/configmap.yaml
ARGO_PROD_MANIFEST_PATH=apps/gs-inv/prod/deployment-app.yaml
ARGO_PROD_CONFIGMAP_PATH=apps/gs-inv/prod/configmap.yaml
```

Preflight check:
- Run GitHub Actions workflow `Deployment Config Plan Check` (`.github/workflows/deploy_config_check.yaml`) before first live promotion.
- Suggested first run:
  - `target=all`
  - `require_argo_updates=true`

## 4) Environment Manifests

Copy-ready templates in this repo:
- `k8s/templates/dev/`
- `k8s/templates/prod/`
- `k8s/templates/argocd/`

Before use in infra repo:
- Replace `secret.template.yaml` with your real secret workflow.
- Set ingress host/TLS.
- Set storage classes.
- Set image tag strategy for each environment.
- Review network-policy defaults and keep only required ingress/egress for your cluster topology.
  - Default template egress is restricted to:
    - DNS: `53/TCP`, `53/UDP` (kube-dns/coredns in `kube-system`)
    - Web/API: `80/TCP`, `443/TCP`
    - Postgres: `5432/TCP`
    - NTP: `123/UDP`

## 5) Dev Promotion Procedure

1. Create release tag `vX.Y.Z` (or use validated main image for non-release smoke runs).
2. Confirm app workflow produced image tags and digest.
3. Update Dev manifest image tag in infra repo (`apps/gs-inv/dev` path).
4. Merge infra PR.
5. ArgoCD sync Dev application.
6. Verify migration job (`gs-inv-migrate`) succeeded (PreSync hook).
7. Verify app health:
   - pods ready
   - service/ingress reachable
   - Streamlit `/_stcore/health` healthy
8. Execute smoke checks:
   - login/auth flow
   - product/listing read/write
   - sync worker running and no fatal errors

## 6) Prod Promotion Procedure

1. Use the exact same image tag validated in Dev.
2. Update Prod manifest image tag in infra repo (`apps/gs-inv/prod` path).
3. Open PR with release notes + rollback tag reference.
4. Obtain required approvals.
5. Merge PR and sync Prod via ArgoCD.
6. Verify migration job success and app rollout status.
7. Run post-deploy checks:
   - health endpoint
   - key pages load
   - eBay workspace auth check
   - sync worker job loop stable

## 7) Rollback Procedure

1. Identify last known good image tag.
2. Revert Prod manifest tag in infra repo to that tag.
3. Merge rollback PR.
4. Sync ArgoCD Prod application.
5. Verify health and workflow checks.
6. Capture incident notes and follow-up actions.

## 8) Operational Gates

Dev gate:
- migrations passed
- smoke checks passed
- no blocker alerts in logs/system health

Prod gate:
- Dev gate already passed on same image tag
- change approved
- rollback tag prepared

## 9) eBay Sync Network Incidents

Symptoms:
- sync worker logs include `NameResolutionError`, `Failed to resolve 'api.ebay.com'`, `No address associated with hostname`, `network is unreachable`, or connection timeouts/resets
- `ebay_connection_health_check` reports `partial` with transient-network warning details
- `ebay_orders_pull_import` records `status=skipped`, `records_failed=0`, and sync error code `EBAY_NETWORK_UNAVAILABLE`
- eBay OAuth auto-refresh records `ebay_oauth / auto_refresh` warning telemetry and enters the configured refresh-failure cooldown without sending Slack auth-failure alerts
- System Health shows `eBay Network Holds`, and Sync highlights unresolved `EBAY_NETWORK_UNAVAILABLE` exceptions

Operator response:
1. Treat this as infrastructure/network/DNS first, not token revocation or eBay credential failure.
2. Check host/container DNS resolution and egress:
   - Docker: `docker compose exec app getent hosts api.ebay.com`
   - Kubernetes: verify DNS egress (`53/TCP`, `53/UDP`) and HTTPS egress (`443/TCP`) are allowed by NetworkPolicy.
3. Check whether other external integrations are also failing; if yes, escalate as broader DNS/egress incident.
4. Do not rotate eBay OAuth credentials unless health checks continue failing after DNS/egress is healthy.
5. After network recovery, either wait for the next sync-runner pass or run manual eBay health/order import from Admin/Sync.
6. Attach sync run IDs, integration events, and DNS/egress evidence to the incident notes.

## 10) Build Metadata Visibility

Recommended next step:
- expose `APP_BUILD_VERSION` and `APP_BUILD_SHA` in runtime config and surface in Admin/System Health for environment traceability.
