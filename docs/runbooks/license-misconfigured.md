# Runbook: License is misconfigured (audit warning fired)

## Symptom

- `LicenseHasNoAuth` alert: `ingress_license_config_warnings{issue="no_auth"} == 1`
- Less severe: `issue="no_hmac"`, `issue="no_secret"`, or `issue="rotation_active"`

## Why this matters

A license with `issue="no_auth"` accepts **unauthenticated** webhooks
from anyone who knows or guesses the license ID. Since license IDs are
UUIDs (effectively unguessable), the immediate risk is low — but the
moment that UUID leaks (logs, screenshots, a Discord post), anyone can
place trades on the customer's broker account.

`no_hmac` and `no_secret` are weaker — the license has one auth
mechanism but not the recommended belt-and-braces both.

`rotation_active` means an HMAC rotation was started but never confirmed,
leaving two HMAC secrets valid. Not a security risk by itself but should
be cleaned up.

## Triage

```sh
# Which license_id(s) and which issue(s)?
curl -s http://localhost:8081/metrics | grep ingress_license_config_warnings
# Sample:
# ingress_license_config_warnings{license_id="60a...",issue="no_auth"} 1
# ingress_license_config_warnings{license_id="60b...",issue="no_hmac"} 1
```

For each affected license, find the owner:

```sh
docker compose exec postgres psql -U execrelay -d execrelay -c "
  SELECT l.id, l.license_key, u.email
  FROM licenses l JOIN users u ON u.id = l.user_id
  WHERE l.id IN ('60a...', '60b...');
"
```

## Mitigation

### `no_auth` — highest priority

Two options. **Pick one and act fast**:

**Option A: Disable the license immediately** (drastic; cuts customer off):

```sh
docker compose exec postgres psql -U execrelay -d execrelay -c "
  UPDATE licenses SET active=false WHERE id='60a...';
"
# Hot-reload ingress to pick up the change:
docker compose exec ingress kill -HUP 1
```

**Option B: Add a secret + HMAC, contact customer to update their
producer**:

```sh
# Generate secrets
SECRET=$(openssl rand -hex 16)
HMAC=$(openssl rand -hex 32)

# Apply
docker compose exec postgres psql -U execrelay -d execrelay -c "
  UPDATE licenses
  SET secret = '$SECRET', hmac_secret = '$HMAC'
  WHERE id='60a...';
"
docker compose exec ingress kill -HUP 1

# Tell the customer the new credentials (out-of-band — email, secure DM)
# Their existing alerts will start getting 401 secret_rejected / signature_rejected
# until they update.
```

Option A is the right call if the license has been compromised or you
have no way to reach the customer.

### `no_hmac` or `no_secret`

Less urgent — the license has *one* auth mechanism, just not both.
Email the customer with the recommendation; document a deadline if
your org has a policy.

The portal's "License → Edit" page should let them roll the missing
secret themselves; if not, manual UPDATE as above.

### `rotation_active` — should be cleaned up

The customer started HMAC rotation but didn't confirm. After they've
updated their producer to use the new key, call:

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/licenses/$LICENSE_ID/confirm-rotation
```

This promotes `pending_hmac_secret` to `hmac_secret` and clears the
pending column. The gauge will clear on the next SIGHUP reload (or
immediately if you trigger one).

## Hot-reload trick

The ingress reads license config from env at startup. To pick up DB
changes (since the portal writes to DB but ingress reads from env),
the deployment needs to either:

1. **Restart ingress** (simple but drops connections):
   ```sh
   docker compose restart ingress
   ```
2. **SIGHUP** if you've wired up env reload from a file path
   (`EXECRELAY_LICENSES_FILE` watched on SIGHUP). See
   [`apps/ingress/cmd/ingress/main.go`](../../apps/ingress/cmd/ingress/main.go).

In the helm chart, a CronJob can reload by sending SIGHUP to the
ingress pod. Direct DB → ingress sync (the portal writing to a
ConfigMap that ingress watches) is the cleaner future state.
<!-- TODO: implement portal-api → ingress reload mechanism.
Today the audit will warn but a portal change won't take effect until
ingress restart. -->

## Root cause checklist

- [ ] How did the license end up with no auth? (Default in the create
      flow? Customer cleared it? Migration left it that way?)
- [ ] Is the create-license API enforcing at-least-one auth mechanism?
- [ ] Are we surfacing this warning to the *customer* in the portal, or
      only to operators in Prometheus?
- [ ] How long has this license been in the misconfigured state? (Check
      the metric's history in Grafana.)

## Postmortem prompts

- Should the portal refuse to create a license without HMAC + secret?
- Should `no_auth` cause the license to automatically deactivate after
  N hours of warning?
- Are we alerting fast enough? The metric is a gauge; it stays at 1
  until cleared. Default Prometheus eval is 15s, so alert latency is
  tight already.
