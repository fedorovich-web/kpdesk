# Billing transition 0050 — production runbook

The 0048 Lava billing ledger is already installed. Version 0050 adds **one
time-limited legacy exception** without falsifying invoices, payment events or
entitlements. It is a separate, reviewed and backup-gated schema migration.

## Design and invariants

- Only owner-role release operators can create a legacy grace record.
- The operator refuses anything other than exactly one existing organization,
  3 configured offers, zero pre-existing grace records and zero active paid
  grants. No organization identifier is guessed or accepted from a browser.
- Grace lasts **at most 30 days**, cannot silently renew, and is revocable.
- Active paid grants take precedence over grace; unexpired grace is checked
  before the normal quote, catalog and membership quota triggers.
- The billing summary exposes `grace_ends_at` separately from subscription.
- No payment ledger rows are created for complimentary access.
- A live production backup must be both verified and restored into an isolated
  PostgreSQL 17 instance before production mutation.

## Rollout order

1. **Keep** `KP_DESK_LAVA_ENABLED=false` in Dokploy and
   `public.billing_config.enabled=false` in PostgreSQL.
2. Deploy the application release that recognizes **both 0049 and 0050**.
   Ensure health and Yandex OAuth still work.
3. Create and independently restore a **new** pre-0050 PostgreSQL backup. Verify
   its SHA-256 and run migration/grace replay on a disposable, internal Docker
   network. Never use production API keys inside that replay.
4. Schedule a short maintenance window, stop the app, and invoke the
   reviewed owner-only migration operator:
   `python -m scripts.upgrade_yandex --target 0050_billing_grace --apply
   --confirm-maintenance --confirm-backup-restored --backup <mount>
   --backup-sha256 <hash>`.
5. Verify revision `0050_billing_grace`, table count 59 and required
   restricted-role privileges; **do not enable billing**.
6. Run `python -m scripts.grant_billing_grace` in read-only mode; then, after
   verifying the sole existing organization and the previously restored dump,
   run `--apply --confirm-single-existing-organization --days 30
   --backup <mount> --backup-sha256 <hash>`. Verify one live grace and
   zero fabricated paid orders.
7. Resume app and verify `/api/health/ready`, Yandex login and browsing.
8. Only after webhook URL/authentication and payment readback are verified,
   schedule a **separate synchronized activation** for PostgreSQL billing
   and Dokploy environment. Never toggle just one setting to true.
9. Run a controlled real payment, reconciliation and refund test. Existing
   grace must not be misrepresented as a paid subscription.

## Failure handling

- Before the schema upgrade, keep 0049 and disabled billing.
- If the migration fails, restore app availability and investigate; do not
  rerun indiscriminately. A post-commit verification failure is **not**
  proof that schema changes were rolled back.
- If grant insertion fails, billing remains disabled, which preserves existing
  create/edit workflows. Do not forge orders or grants to unblock the user.
- Once billing is active, turn it off in a controlled maintenance operation
  before attempting to reverse application configuration. Preserve audit data.
