# Teamspace demo PKI

A deliberately tiny certificate authority so the demo can run with TLS
verification **enabled** instead of `verify=False` / `--insecure` everywhere.

**What it is:** one root CA and five server certificates, generated locally with
OpenSSL. The keys are throwaway and live in `out/`, which is git-ignored.

**What it is not:** this is not a PKI showcase. No intermediates, no CRL, no
OCSP, no client certificates, no mTLS, no revocation. Never use any of this
outside the demo.

## Why a CA at all

WSO2 IS ships a self-signed certificate with `CN=localhost` and
`SAN = DNS:localhost`. Java inside IS rejected `host.docker.internal` with
*"HTTPS hostname wrong"*, because hostname verification uses the SAN and
ignores the CN.

Each service here is reachable under several names, and **all of them** must be
in the certificate's SAN or the same bug comes back:

| Name | Used by |
| --- | --- |
| `<service>.test` | the browser and the host |
| `localhost` / `127.0.0.1` | host-mode runs and the `setup_*.py` scripts |
| compose service name (`webapp`, `ai-agent`, `business-api`) | container-to-container over the compose network |
| `host.docker.internal` | containers reaching something on the macOS host |

A single root CA also means the two IS instances trust each other: importing
one CA certificate into each `client-truststore.p12` covers the federated login
hops. With self-signed certificates every party would need every other party's
leaf.


## Hostnames

| Hostname | Service | Port | Compose service |
| --- | --- | --- | --- |
| `identityserver.test` | primary WSO2 IS | 9443 | — (host) |
| `secondaryidp.test` | federated WSO2 IS | 9444 | — (host) |
| `flaskapp.test` | Flask portal | 5001 | `webapp` |
| `aiagent.test` | AI agent | 8000 | `ai-agent` |
| `businessapi.test` | Business API | 9091 | `business-api` |

## Order of operations

1. Generate everything:

   ```bash
   cd pki
   ./generate.sh
   ```

2. Patch the primary IS (backs up both keystores first):

   ```bash
   ./apply_to_is.sh --is-path /path/to/primary-wso2is-7.2.0.x --cert identityserver
   ```

3. Patch the federated IS:

   ```bash
   ./apply_to_is.sh --is-path /path/to/federated-wso2is-7.2.0.x --cert secondaryidp
   ```

4. Set `[server] hostname` in each `repository/conf/deployment.toml` to match
   the certificate, then restart both:

   ```toml
   # primary
   [server]
   hostname = "identityserver.test"

   # federated
   [server]
   hostname = "secondaryidp.test"
   ```

5. Add to `/etc/hosts`:

   ```
   127.0.0.1 identityserver.test secondaryidp.test flaskapp.test aiagent.test businessapi.test
   ```

6. Point the services at the CA bundle (absolute paths — containers and the
   host resolve them differently if you use a relative one):

   ```bash
   IS_VERIFY_TLS=/abs/path/to/repo/pki/out/ca-bundle.crt
   SERVICE_VERIFY_TLS=/abs/path/to/repo/pki/out/ca-bundle.crt
   ```

   `out/ca-bundle.crt` is just the root CA certificate. It is the only file
   clients need, and the only one that has to be mounted into containers.

## Serving the app services over TLS

`out/<name>/<name>.fullchain.pem` is the leaf followed by the CA certificate.
Use the fullchain, not the bare `.crt`: gunicorn sends exactly what
`--certfile` contains, and a client that only trusts the root cannot build a
path from the leaf alone.

| Service | `--certfile` | `--keyfile` |
| --- | --- | --- |
| `webapp` (Flask portal) | `pki/out/flaskapp/flaskapp.fullchain.pem` | `pki/out/flaskapp/flaskapp.key` |
| `ai-agent` | `pki/out/aiagent/aiagent.fullchain.pem` | `pki/out/aiagent/aiagent.key` |
| `business-api` | `pki/out/businessapi/businessapi.fullchain.pem` | `pki/out/businessapi/businessapi.key` |

The two WSO2 IS certificates additionally get `out/<name>/<name>.p12` (alias and
password `wso2carbon`, WSO2's defaults) because Java keystores cannot import a
PEM keypair. `apply_to_is.sh` consumes those.

## Optional: trust the CA in the browser (macOS)

```bash
sudo security add-trusted-cert -d -r trustRoot \
    -k /Library/Keychains/System.keychain pki/out/ca-bundle.crt
```

Entirely optional — clicking through the browser warning works too. Only the
service-to-service calls need `IS_VERIFY_TLS` / `SERVICE_VERIFY_TLS`.

To undo it later:

```bash
sudo security delete-certificate -c "Teamspace Demo Root CA" /Library/Keychains/System.keychain
```

## Regenerating

`./generate.sh` refuses to run if `out/` exists, because a new root CA silently
invalidates the keystores already imported into the WSO2 installs and any trust
added to the keychain. If you really mean it:

```bash
./generate.sh --force
# then re-run apply_to_is.sh for BOTH IS installs and restart them
```

`out/` is generated and git-ignored (see `pki/.gitignore`) — no private key from
this directory can be committed.
