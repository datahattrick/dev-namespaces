"""Delete expired developer namespaces.

Configuration comes from environment variables set by the CronJob. Uses only
the Python standard library and the in-cluster ServiceAccount credentials.

Every log line is one JSON object with a stable `event` field; the README
lists the events and their fields.
"""

import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
TENANT_LABEL = "capsule.clastix.io/tenant"
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}

# Namespace conditions that explain why a deletion has not finished.
DELETION_CONDITIONS = {
    "NamespaceDeletionDiscoveryFailure",
    "NamespaceDeletionGroupVersionParsingFailure",
    "NamespaceDeletionContentFailure",
    "NamespaceContentRemaining",
    "NamespaceFinalizersRemaining",
}

LOG_LEVEL = LEVELS.get(os.environ.get("REAPER_LOG_LEVEL", "info"), 20)
RUN_ID = os.environ.get("HOSTNAME", "local")
DRY_RUN = os.environ.get("REAPER_DRY_RUN") == "true"


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(level, event, msg, **fields):
    if LEVELS[level] < LOG_LEVEL:
        return
    record = {
        "ts": iso(now()),
        "level": level,
        "event": event,
        "msg": msg,
        "component": "devspaces-reaper",
        "run_id": RUN_ID,
        "dry_run": DRY_RUN,
    }
    record.update(fields)
    print(json.dumps(record, default=str), flush=True)


def parse_duration(value):
    """Parse '90m', '36h', '7d', '2w' into a timedelta. Returns None if invalid."""
    m = re.fullmatch(r"(\d+)([smhdw])", value.strip())
    if not m:
        return None
    return dt.timedelta(seconds=int(m.group(1)) * UNITS[m.group(2)])


def parse_ts(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class APIError(Exception):
    """A failed API call, carrying the Kubernetes Status reason and message."""

    def __init__(self, status, reason, message):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message

    def fields(self):
        return {"http_status": self.status, "api_reason": self.reason, "api_message": self.message}


class Client:
    def __init__(self):
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT"]
        if ":" in host:
            host = f"[{host}]"
        self.base = f"https://{host}:{port}"
        self.ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    def request(self, method, path, body=None, content_type="application/json"):
        with open(f"{SA_DIR}/token") as f:
            token = f.read().strip()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                status = json.loads(raw)
                raise APIError(e.code, status.get("reason", ""), status.get("message", raw[:500])) from None
            except ValueError:
                raise APIError(e.code, "", raw[:500]) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise APIError(0, "ConnectionError", str(e)) from None

    def list_namespaces(self, selector):
        items, cont = [], ""
        while True:
            q = {"labelSelector": selector, "limit": "500"}
            if cont:
                q["continue"] = cont
            page = self.request("GET", "/api/v1/namespaces?" + urllib.parse.urlencode(q))
            items.extend(page.get("items", []))
            cont = page.get("metadata", {}).get("continue", "")
            if not cont:
                return items

    def get_namespace(self, name):
        """Return the namespace, or None if it no longer exists."""
        try:
            return self.request("GET", f"/api/v1/namespaces/{name}")
        except APIError as e:
            if e.status == 404:
                return None
            raise


def ns_fields(ns):
    """Identifying fields logged with every namespace event."""
    meta = ns["metadata"]
    created = parse_ts(meta["creationTimestamp"])
    return {
        "namespace": meta["name"],
        "tenant": meta.get("labels", {}).get(TENANT_LABEL, ""),
        "uid": meta["uid"],
        "created_at": iso(created),
        "age_seconds": int((now() - created).total_seconds()),
    }


def deletion_details(ns):
    """What is holding up a terminating namespace."""
    meta = ns["metadata"]
    fields = {
        "deletion_requested_at": meta.get("deletionTimestamp", ""),
        "finalizers": ns.get("spec", {}).get("finalizers", []) + meta.get("finalizers", []),
        "conditions": [
            {"type": c["type"], "reason": c.get("reason", ""), "message": c.get("message", "")}
            for c in ns.get("status", {}).get("conditions", [])
            if c.get("type") in DELETION_CONDITIONS and c.get("status") == "True"
        ],
    }
    if meta.get("deletionTimestamp"):
        fields["terminating_seconds"] = int((now() - parse_ts(meta["deletionTimestamp"])).total_seconds())
    return fields


def expiry_of(ns, cfg, counts):
    """Return (expiry datetime, reason) for a namespace."""
    meta = ns["metadata"]
    labels = meta.get("labels", {})
    created = parse_ts(meta["creationTimestamp"])
    ceiling = created + cfg["max_ttl"]

    key = cfg["key_ttl"]
    if key in labels:
        ttl = parse_duration(labels[key])
        if ttl is not None:
            value = created + ttl
            return min(value, ceiling), "ttl-label" + ("-capped" if value > ceiling else "")
        counts["invalid_labels"] += 1
        log("warning", "label_invalid", "ignoring invalid label value",
            label=key, value=labels[key], **ns_fields(ns))

    return min(created + cfg["default_ttl"], ceiling), "default-ttl"


def config():
    def duration(name):
        value = parse_duration(os.environ[name])
        if value is None:
            raise ValueError(f"{name}={os.environ[name]!r} is not a valid duration")
        return value

    cfg = {
        "selector": os.environ["REAPER_SELECTOR_LABEL"].strip(),
        "annotate": os.environ.get("REAPER_ANNOTATE_EXPIRY") == "true",
        "default_ttl": duration("REAPER_DEFAULT_TTL"),
        "max_ttl": duration("REAPER_MAX_TTL"),
        "stuck_after": duration("REAPER_STUCK_AFTER"),
        "delete_wait": int(os.environ["REAPER_DELETE_WAIT_SECONDS"]),
        "fail_on_stuck": os.environ.get("REAPER_FAIL_ON_STUCK") == "true",
        "max_deletions": int(os.environ["REAPER_MAX_DELETIONS"]),
        "protected": set(filter(None, os.environ.get("REAPER_PROTECTED", "").split(","))),
        "protected_re": re.compile(os.environ["REAPER_PROTECTED_REGEX"]) if os.environ.get("REAPER_PROTECTED_REGEX") else None,
        "key_ttl": os.environ["REAPER_KEY_TTL"],
        "key_delete": os.environ["REAPER_KEY_DELETE"],
        "key_expires_at": os.environ["REAPER_KEY_EXPIRES_AT"],
    }
    if not cfg["selector"]:
        raise ValueError("REAPER_SELECTOR_LABEL is empty")
    if cfg["default_ttl"] > cfg["max_ttl"]:
        raise ValueError("default TTL exceeds max TTL")
    return cfg


def main():
    try:
        cfg = config()
    except (KeyError, ValueError, re.error) as e:
        log("error", "config_invalid", "invalid configuration", error=str(e))
        return 2

    counts = dict.fromkeys(
        ["scanned", "kept", "protected", "terminating", "expired", "deleted", "pending",
         "delete_failed", "stuck", "annotated", "annotate_failed", "invalid_labels"], 0)
    started = time.monotonic()
    client = Client()
    # Any namespace carrying the label, whatever its value.
    selector = cfg["selector"]
    log("info", "run_started", "reaper run started", selector=selector,
        default_ttl=os.environ["REAPER_DEFAULT_TTL"], max_ttl=os.environ["REAPER_MAX_TTL"])

    try:
        namespaces = client.list_namespaces(selector)
    except APIError as e:
        log("error", "list_failed", "could not list namespaces", **e.fields())
        return 1

    expired = []
    current = now()
    for ns in namespaces:
        meta = ns["metadata"]
        name = meta["name"]
        counts["scanned"] += 1

        if name in cfg["protected"] or (cfg["protected_re"] and cfg["protected_re"].search(name)):
            counts["protected"] += 1
            log("debug", "namespace_skipped", "namespace is protected", skip_reason="protected", **ns_fields(ns))
            continue

        if meta.get("deletionTimestamp"):
            counts["terminating"] += 1
            details = deletion_details(ns)
            if details["terminating_seconds"] >= cfg["stuck_after"].total_seconds():
                counts["stuck"] += 1
                log("error", "namespace_deletion_stuck",
                    "namespace has been terminating longer than the stuck threshold",
                    stuck_after_seconds=int(cfg["stuck_after"].total_seconds()), **ns_fields(ns), **details)
            else:
                log("debug", "namespace_terminating", "namespace is terminating", **ns_fields(ns), **details)
            continue

        delete = meta.get("labels", {}).get(cfg["key_delete"])
        if delete == "false":
            counts["kept"] += 1
            log("debug", "namespace_skipped", "namespace is marked delete=false", skip_reason="delete-false", **ns_fields(ns))
            continue
        if delete not in (None, "true"):
            counts["invalid_labels"] += 1
            log("warning", "label_invalid", "ignoring invalid label value",
                label=cfg["key_delete"], value=delete, **ns_fields(ns))

        expiry, reason = expiry_of(ns, cfg, counts)
        fields = {**ns_fields(ns), "expires_at": iso(expiry), "expiry_reason": reason}

        if current >= expiry:
            counts["expired"] += 1
            expired.append((expiry, ns, fields))
            continue

        log("debug", "namespace_evaluated", "namespace not yet expired",
            expires_in_seconds=int((expiry - current).total_seconds()), **fields)

        if cfg["annotate"] and not DRY_RUN and meta.get("annotations", {}).get(cfg["key_expires_at"]) != iso(expiry):
            try:
                client.request(
                    "PATCH",
                    f"/api/v1/namespaces/{name}",
                    {"metadata": {"annotations": {cfg["key_expires_at"]: iso(expiry)}}},
                    "application/merge-patch+json",
                )
                counts["annotated"] += 1
                log("info", "expiry_annotated", "expiry annotation updated", **fields)
            except APIError as e:
                counts["annotate_failed"] += 1
                log("error", "annotate_failed", "could not write expiry annotation", **fields, **e.fields())

    # Oldest expiry first, so a deletion limit removes the most overdue first.
    expired.sort(key=lambda item: item[0])
    requested = []
    for i, (expiry, ns, fields) in enumerate(expired):
        if i >= cfg["max_deletions"]:
            log("warning", "delete_limit_reached", "deletion limit reached; remaining namespaces wait for the next run",
                limit=cfg["max_deletions"], remaining=len(expired) - i,
                remaining_namespaces=[f["namespace"] for _, _, f in expired[i:]])
            break
        fields["overdue_seconds"] = int((current - expiry).total_seconds())
        if DRY_RUN:
            log("info", "namespace_would_delete", "dry run: namespace would be deleted", **fields)
            continue
        body = {
            "kind": "DeleteOptions",
            "apiVersion": "v1",
            "propagationPolicy": "Background",
            "preconditions": {"uid": fields["uid"]},
        }
        try:
            client.request("DELETE", f"/api/v1/namespaces/{fields['namespace']}", body)
            requested.append((time.monotonic(), fields))
            log("info", "namespace_delete_requested", "namespace deletion requested", **fields)
        except APIError as e:
            if e.status == 404:
                log("info", "namespace_already_gone", "namespace disappeared before deletion", **fields)
                continue
            counts["delete_failed"] += 1
            log("error", "namespace_delete_failed", "namespace deletion was refused", **fields, **e.fields())

    # Confirm deletions; anything left is reported with what is blocking it.
    deadline = time.monotonic() + cfg["delete_wait"]
    while requested:
        remaining = []
        for t0, fields in requested:
            try:
                ns = client.get_namespace(fields["namespace"])
            except APIError as e:
                log("warning", "namespace_status_unknown", "could not check deletion progress", **fields, **e.fields())
                ns = {"metadata": {}}
            if ns is None or ns["metadata"].get("uid") not in (None, fields["uid"]):
                counts["deleted"] += 1
                log("info", "namespace_deleted", "namespace deleted",
                    deletion_seconds=int(time.monotonic() - t0), **fields)
            else:
                remaining.append((t0, fields, ns))
        if not remaining or time.monotonic() >= deadline:
            for t0, fields, ns in remaining:
                counts["pending"] += 1
                log("warning", "namespace_deletion_pending",
                    "namespace still terminating after the wait; later runs report it as stuck past the threshold",
                    waited_seconds=int(time.monotonic() - t0), **fields, **deletion_details(ns))
            break
        requested = [(t0, f) for t0, f, _ in remaining]
        time.sleep(5)

    failed = counts["delete_failed"] + counts["annotate_failed"] + (counts["stuck"] if cfg["fail_on_stuck"] else 0)
    log("error" if failed else "info", "run_finished", "reaper run finished",
        duration_seconds=round(time.monotonic() - started, 1), **counts)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
