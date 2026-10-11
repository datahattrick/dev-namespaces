# capsule-devspaces

Umbrella chart that deploys the [Capsule](https://projectcapsule.dev/) controller so cluster users can create their own namespaces,
and a CronJob that deletes those namespaces when they expire.

- **Controller only.** Capsule proxy, dashboards, monitoring and the ResourcePool webhooks are off.
- **Hardened controller RBAC.** Replaces upstream's `cluster-admin` binding. See [Controller RBAC](#controller-rbac).
- **ClusterRoles, ClusterRoleBindings, GlobalTenantResources, GlobalResourceQuotas and GlobalCustomQuotas** from values.
  Tenants and TenantOwners are expected to be managed elsewhere; `tenants` is empty by default.
- **Custom quota enforcement.** GlobalCustomQuota limits checked at admission. See [Custom quotas](#custom-quotas).
- **Namespace reaper.** Deletes developer namespaces by age and labels. See [Reaper](#reaper).

## Requirements

1. Kubernetes 1.30 or later (OpenShift 4.17 or later), for `ValidatingAdmissionPolicy`.
2. cert-manager, or set `capsule.certManager.generateCertificates=false`, `capsule.tls.create=true` and `capsule.tls.enableController=true`.

## Install

```bash
helm dependency build charts/capsule-devspaces
helm upgrade --install devspaces charts/capsule-devspaces -n capsule-system --create-namespace
```

On OpenShift add `-f charts/capsule-devspaces/examples/openshift-values.yaml`.
It removes the fixed UID `1002` that upstream sets, which the `restricted-v2` SCC rejects.

If the first install fails part way, the controller writes its own `CapsuleConfiguration`, and later upgrades fail with `Apply failed with 4 conflicts: conflicts with "controller"`.
Upgrade once with `--force-conflicts`, then restart the controller.

With Helm 4, upgrade with `--reset-values` or `-f`: with neither, the previous release's values are reused.

## Creating a namespace

Members of `devspaces-users` (`capsule.manager.options.users`) who own a Tenant create namespaces prefixed with the tenant name:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: developers-alice-feature-x
  labels:
    devspaces.io/ttl: 3d
```

Capsule creates the owner RoleBindings a few seconds after the namespace.
Until then the creator gets `cannot get resource "namespaces"`, so set labels in the manifest as above.

For CPU and memory, select namespaces in a ClusterResourceQuota with `capsule.clastix.io/tenant: <tenant>`.

## Tenants

Every ClusterRole a Tenant or TenantOwner grants must be in `controllerRbac.extraBindableClusterRoles` (default `admin`).
Otherwise the owner RoleBinding fails with `is attempting to grant RBAC permissions not currently held`.

For the reaper, a Tenant needs its name in `reaper.tenants` and this rule:

```yaml
spec:
  rules:
    - audience:
        - kind: Custom
          name: CapsuleUser
      enforce:
        action: deny
        metadata:
          - apiGroups: ["v1"]
            kinds: ["Namespace"]
            labels:
              devspaces.io/keep:
                required: false
                values: [{exp: "^.*$"}]
            annotations:
              devspaces.io/expires-at:
                required: false
                values: [{exp: "^.*$"}]
```

Without it, owners can exempt their own namespaces from deletion.
With it, owners can still remove a `keep` label an administrator set.
`namespaceOptions.forbiddenLabels` does the same and is deprecated.

## Custom quotas

Each resource a GlobalCustomQuota counts is listed once, in the calculation webhook's rules.
That list also grants the controller `get`, `list` and `watch` on those resources.

```yaml
globalCustomQuotas:
  devspaces-jobs:
    spec:
      limit: "2"
      options: {}
      namespaceSelectors:
        - matchLabels:
            capsule.clastix.io/tenant: developers
      sources:
        - apiVersion: batch/v1
          kind: Job
          op: count

capsule:
  webhooks:
    hooks:
      calculations:
        enabled: true
        rules:
          - apiGroups: ["batch"]
            apiVersions: ["v1"]
            operations: ["CREATE", "UPDATE", "DELETE"]
            resources: ["jobs"]
            scope: Namespaced
```

- With the webhook enabled, a request over the limit fails with `creating resource exceeds limit for GlobalCustomQuota "devspaces-jobs" (requested=1, currentUsed=3, ...)`. `currentUsed` includes the request.
- With it disabled, the quota only reports usage in its status.
- The webhook only matches tenant namespaces (`capsule.clastix.io/tenant` exists). Upstream matches every namespace, and with `failurePolicy: Fail` a controller outage would block the counted kinds cluster-wide.
- The chart fails to render when `globalCustomQuotas` is set without rules, or the webhook is enabled without rules.
- GlobalCustomQuotas managed outside the chart need their resources in the same rules. Otherwise the controller logs `jobs.batch is forbidden` and keeps running.
- Status usage lags admission by a few seconds.
- After removing a resource from the rules, restart the controller. Until then it logs `Failed to watch` with `is forbidden` every few seconds.

## Reaper

Runs every 15 minutes.
A namespace in `reaper.tenants` expires at its creation time plus `reaper.defaultTTL` (7d), unless a label overrides it:

| Label | Value | Effect |
|---|---|---|
| `devspaces.io/ttl` | `90m`, `36h`, `3d`, `2w` | Lifetime from creation |
| `devspaces.io/expires-on` | `2026-11-30` | Delete after the end of this UTC day |
| `devspaces.io/delete` | `true` | Delete on the next run |
| `devspaces.io/keep` | `true` | Never delete |

- Overrides are capped at `reaper.maxTTL` (30d) from creation.
- An invalid value is logged and the default TTL applies.
- Tenant owners cannot set `devspaces.io/keep` when the Tenant forbids it (see [Tenants](#tenants)); an administrator sets it.
- The computed expiry is written to the `devspaces.io/expires-at` annotation.
- At most `reaper.maxDeletionsPerRun` (25) namespaces are deleted per run.
- `reaper.dryRun: true` logs deletions without making them.

A `ValidatingAdmissionPolicy` limits the reaper's ServiceAccount to deleting, or setting the `expires-at` annotation on, namespaces of `reaper.tenants` without `keep=true`.

To run it now:

```bash
kubectl -n capsule-system create job reaper-manual --from=cronjob/devspaces-reaper
kubectl -n capsule-system logs -f job/reaper-manual
```

### Reaper logs

One JSON object per line.
Alert on `event`; `msg` is for people.
Every line has `ts`, `level`, `event`, `msg`, `component` (`devspaces-reaper`), `run_id` (pod name) and `dry_run`.
Namespace events add `namespace`, `tenant`, `uid`, `created_at` and `age_seconds`.

| `event` | Level | Meaning | Extra fields |
|---|---|---|---|
| `run_started` | info | Run began | `selector`, `tenants`, `default_ttl`, `max_ttl` |
| `namespace_delete_requested` | info | API accepted the deletion | `expires_at`, `expiry_reason`, `overdue_seconds` |
| `namespace_deleted` | info | Namespace confirmed gone | as above, `deletion_seconds` |
| `namespace_would_delete` | info | Dry run only | as `namespace_delete_requested` |
| `expiry_annotated` | info | `expires-at` annotation changed | `expires_at`, `expiry_reason` |
| `namespace_deletion_pending` | warning | Still terminating after `reaper.deleteWaitSeconds` | `finalizers`, `conditions`, `terminating_seconds` |
| `delete_limit_reached` | warning | `maxDeletionsPerRun` hit | `remaining`, `remaining_namespaces` |
| `label_invalid` | warning | Label value ignored; logged every run until fixed | `label`, `value` |
| `namespace_deletion_stuck` | error | Tenant namespace terminating longer than `reaper.stuckAfter` (30m); logged every run | `finalizers`, `conditions`, `terminating_seconds` |
| `namespace_delete_failed` | error | API refused the deletion | `http_status`, `api_reason`, `api_message` |
| `annotate_failed` | error | Annotation write refused | `http_status`, `api_reason`, `api_message` |
| `list_failed` | error | Could not list namespaces; run aborted | `http_status`, `api_reason`, `api_message` |
| `config_invalid` | error | Bad environment; run aborted | `error` |
| `run_finished` | info, or error if anything failed | Run summary | `duration_seconds` and counts: `scanned`, `kept`, `protected`, `terminating`, `expired`, `deleted`, `pending`, `delete_failed`, `stuck`, `annotated`, `annotate_failed`, `invalid_labels` |

`expiry_reason` is one of `default-ttl`, `ttl-label`, `expires-on-label` or `delete-label`; `-capped` is appended when `maxTTL` applied.

`conditions` carries the namespace's own deletion conditions, for example:

```json
{"type": "NamespaceFinalizersRemaining", "reason": "SomeFinalizersRemain",
 "message": "Some content in the namespace has finalizers remaining: example.com/block in 1 resource instances"}
```

`namespace_deletion_stuck` covers every terminating namespace of `reaper.tenants`, including ones deleted by their owners.

The Job fails, with no retry, when a run logs `namespace_delete_failed`, `annotate_failed`, `list_failed` or `config_invalid`, or `namespace_deletion_stuck` while `reaper.failOnStuck` is `true`, so a failed-Job alert covers these.
`reaper.logLevel: debug` adds a line per namespace per run: `namespace_evaluated`, `namespace_skipped` (`skip_reason`: `keep` or `protected`) and `namespace_terminating`.

## Controller RBAC

`capsule.manager.rbac.create` is `false`, and `templates/rbac/controller.yaml` binds the controller to a ClusterRole that:

- Reads only the kinds Capsule's admission checks use, plus those in `capsule.webhooks.hooks.calculations.rules`; secrets only in its own namespace.
- Writes LimitRanges, ResourceQuotas, NetworkPolicies and RoleBindings, and metadata on namespaces, pods, services, PersistentVolumes and EndpointSlices.
- Can `bind` only the ClusterRoles in `controllerRbac.extraBindableClusterRoles`, `capsule.manager.options.rbac` and any `tenants` in values.
- Updates and deletes only its own webhook configurations and the `capsule-namespace-provisioner` and `capsule-namespace-deleter` ClusterRoles and bindings.
- Cannot impersonate.

GlobalTenantResources are replicated with the controller's own permissions.
A GlobalTenantResource for a kind other than LimitRange, ResourceQuota, NetworkPolicy or RoleBinding fails with `is forbidden: User "system:serviceaccount:capsule-system:capsule"` until the kind is added to `controllerRbac.extraRules`.
A GlobalTenantResource or TenantResource that sets `serviceAccount` fails with `evaluating managed metadata: failed to get server groups: unknown`.
So does a TenantResource without one, since upstream defaults `tenantDefaultServiceAccount` to `default` and this chart cannot unset it.

A Tenant rule that references another kind needs read access in `controllerRbac.extraRules`.

The controller also holds namespace `create`, which Kubernetes requires before it lets the controller write the provisioner ClusterRole.

Upstream also grants list, patch and delete on every resource to clear finalizers in terminating tenant namespaces.
That is `controllerRbac.allowCascadingCleanup`, off by default.
Without it the controller logs `replicationcontrollers is forbidden` and similar while a tenant namespace terminates, and a namespace stuck on a finalizer needs an administrator.

## Operations

- Tenant changes go through Capsule's webhook, so `helm upgrade` fails with `no endpoints available for service "capsule-webhook-service"` while the controller is down.
- The ResourcePool controllers still run and their CRDs are installed; Capsule has no switch to disable them.
- Capsule's `managed` webhook lets the Kubernetes garbage collector delete objects Capsule manages only in terminating namespaces. Without the `allow-garbage-collector-deletes` match condition this chart adds, the QuantityLedgers of a deleted GlobalResourceQuota are never removed, and deleting them by hand fails with `can only be done by the controller or administrators`.
- After a failed upgrade, Helm compares against the last successful release, so objects added in the failed one are left behind and need deleting by hand.

## Tested

On k3s 1.36 with Capsule 0.14.6: namespace self-service and prefix enforcement, owners bound to `admin` on an externally applied Tenant, GlobalTenantResource replication and refusal of impersonation, GlobalResourceQuota and its ledger cleanup, GlobalCustomQuota enforcement through the calculation webhook, controller boot with no quotas and with a quota on an unreadable kind, every reaper label, and the admission policy.

Not tested on OpenShift: ClusterResourceQuota interaction, the SCC override, and `OwnerReferencesPermissionEnforcement` (the chart grants `update` on Capsule `*/finalizers` for it).

## Values

| File | Contents |
|---|---|
| [values.yaml](values.yaml) | All options, with comments |
| [examples/openshift-values.yaml](examples/openshift-values.yaml) | OpenShift UID overrides |
| [ci/test-values.yaml](ci/test-values.yaml) | GlobalTenantResource, GlobalResourceQuota and GlobalCustomQuota used in testing |
