#!/usr/bin/env bash
# Generate the Teamspace demo PKI: one root CA plus one server cert per
# service, so the whole stack can run with TLS verification ENABLED.
#
# Usage:
#   ./generate.sh              # generate into pki/out/ (refuses if it exists)
#   ./generate.sh --force      # wipe pki/out/ and start over
#   ./generate.sh --help
#
# This is a demo CA. The keys are throwaway, there is no CRL, no OCSP, no
# client certs and no mTLS. See README.md.
set -euo pipefail

# Everything is relative to pki/ (openssl.cnf uses relative paths like
# "out/root_ca"), so the script must run from its own directory regardless of
# where the caller invoked it from.
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

OUT="out"
CA_DIR="$OUT/root_ca"
CA_KEY="$CA_DIR/ca.key"
CA_CRT="$CA_DIR/ca.crt"
CA_BUNDLE="$OUT/ca-bundle.crt"
CONF="openssl.cnf"

CA_DAYS=3650
LEAF_DAYS=825
CA_SUBJ="/O=Teamspace Demo/CN=Teamspace Demo Root CA"

# WSO2 IS 7.2.0 ships wso2carbon.p12 with this password and alias. Keeping both
# defaults means deployment.toml needs no keystore changes at all.
P12_PASSWORD="wso2carbon"
P12_ALIAS="wso2carbon"

FORCE=0

usage() {
    # BSD sed has no \? in BREs, so strip the marker and one space separately.
    sed -n '2,11p' "$0" | sed -e 's/^#//' -e 's/^ //'
    exit 0
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

log() {
    echo "  $*"
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found on PATH. $2"
}

# openssl is chatty on success and its real error is usually the last line, so
# swallow the noise but print everything if the command fails.
run_quiet() {
    local output
    if ! output="$("$@" 2>&1)"; then
        printf '%s\n' "$output" >&2
        die "command failed: $*"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force)   FORCE=1 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
    shift
done

need_cmd openssl "Install it (macOS: 'brew install openssl' or use /usr/bin/openssl)."
[ -f "$CONF" ] || die "$HERE/$CONF is missing; this script needs it to sign certificates."

# Regenerating produces a NEW root CA. Any keystore already patched by
# apply_to_is.sh, any container that mounted the old bundle and any browser
# that trusted the old root would all keep the stale CA and start failing with
# confusing verification errors. Make that an explicit choice.
if [ -e "$OUT" ]; then
    if [ "$FORCE" -eq 0 ]; then
        echo "ERROR: $HERE/$OUT already exists." >&2
        echo "       Regenerating creates a NEW root CA, which invalidates the keystores" >&2
        echo "       already imported into your WSO2 IS installs and any trust you added" >&2
        echo "       to the macOS keychain." >&2
        echo >&2
        echo "       To keep what you have:   nothing to do, the PKI is already generated." >&2
        echo "       To start over:           ./generate.sh --force" >&2
        echo "                                then re-run ./apply_to_is.sh for BOTH IS installs." >&2
        exit 1
    fi
    log "removing existing $OUT/ (--force)"
    rm -rf "$OUT"
fi

echo "Teamspace demo PKI"
log "openssl: $(openssl version)"
log "output:  $HERE/$OUT"
echo

# --- root CA ---------------------------------------------------------------
echo "==> Root CA"
mkdir -p "$CA_DIR/newcerts" "$CA_DIR/private"
chmod 700 "$CA_DIR/private"
: > "$CA_DIR/index.txt"
echo 1000 > "$CA_DIR/serial"

# openssl.cnf points at $dir/private/ca.key while the key itself lives at the
# shorter documented path. The symlink keeps a bare `openssl ca -config
# openssl.cnf` working without a second copy of the private key on disk.
run_quiet openssl genrsa -out "$CA_KEY" 4096
chmod 600 "$CA_KEY"
ln -sf ../ca.key "$CA_DIR/private/ca.key"

# $ENV::SAN in [server_cert] is resolved when the config file is *parsed*, not
# when the section is used, so every openssl call that loads openssl.cnf needs
# SAN set — including this one, which does not issue a leaf certificate.
export SAN="DNS:invalid"

run_quiet openssl req -x509 -new -nodes \
    -config "$CONF" \
    -extensions v3_ca \
    -key "$CA_KEY" \
    -sha256 \
    -days "$CA_DAYS" \
    -subj "$CA_SUBJ" \
    -out "$CA_CRT"
log "$CA_KEY (RSA 4096, mode 600)"
log "$CA_CRT (self-signed, ${CA_DAYS}d, CN=Teamspace Demo Root CA)"
echo

# --- server certificates ---------------------------------------------------
# SAN rule for this demo: every name a service can be reached under must be in
# the SAN, or we recreate the bug that started all of this. WSO2 IS ships a
# cert with SAN=DNS:localhost only, and Java inside the container rejected
# host.docker.internal with "HTTPS hostname wrong". Each cert therefore lists:
#   1. the stable .test name        - the browser and the host use it
#   2. localhost + 127.0.0.1         - host-mode runs and the setup_*.py scripts
#   3. the compose service name      - container-to-container over the compose
#      and host.docker.internal        network, and containers reaching the mac
issue_server_cert() {
    local name="$1" cn="$2" san="$3"
    local dir="$OUT/$name"

    mkdir -p "$dir"
    run_quiet openssl genrsa -out "$dir/$name.key" 2048
    chmod 600 "$dir/$name.key"

    # SAN is read from the environment by [server_cert] in openssl.cnf.
    export SAN="$san"

    run_quiet openssl req -new \
        -config "$CONF" \
        -key "$dir/$name.key" \
        -subj "/O=Teamspace Demo/CN=$cn" \
        -out "$dir/$name.csr"

    run_quiet openssl ca -batch -notext \
        -config "$CONF" \
        -extensions server_cert \
        -days "$LEAF_DAYS" \
        -md sha256 \
        -in "$dir/$name.csr" \
        -out "$dir/$name.crt"

    # gunicorn/uvicorn send exactly what --certfile contains, so the leaf alone
    # would leave clients that only trust the root without a path to build.
    cat "$dir/$name.crt" "$CA_CRT" > "$dir/$name.fullchain.pem"

    echo "  $name  CN=$cn"
    echo "         $san"
}

# Java keystores cannot import a bare PEM keypair; PKCS#12 is the format
# keytool -importkeystore understands.
issue_p12() {
    local name="$1"
    local dir="$OUT/$name"

    run_quiet openssl pkcs12 -export \
        -inkey "$dir/$name.key" \
        -in "$dir/$name.crt" \
        -certfile "$CA_CRT" \
        -name "$P12_ALIAS" \
        -passout "pass:$P12_PASSWORD" \
        -out "$dir/$name.p12"
    chmod 600 "$dir/$name.p12"
    echo "         $dir/$name.p12 (alias $P12_ALIAS, password $P12_PASSWORD)"
}

echo "==> Server certificates (RSA 2048, SHA-256, ${LEAF_DAYS}d)"
issue_server_cert identityserver identityserver.test \
    "DNS:identityserver.test,DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1"
issue_p12 identityserver

issue_server_cert secondaryidp secondaryidp.test \
    "DNS:secondaryidp.test,DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1"
issue_p12 secondaryidp

issue_server_cert flaskapp flaskapp.test \
    "DNS:flaskapp.test,DNS:webapp,DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1"

issue_server_cert aiagent aiagent.test \
    "DNS:aiagent.test,DNS:ai-agent,DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1"

issue_server_cert businessapi businessapi.test \
    "DNS:businessapi.test,DNS:business-api,DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1"
echo

# --- trust bundle ----------------------------------------------------------
# One root, no intermediates, so the bundle is just the root certificate.
# requests/httpx take a file path here, and the same file is what containers
# mount to verify the IS and each other.
cp "$CA_CRT" "$CA_BUNDLE"
chmod 644 "$CA_BUNDLE"

cat <<EOF
==> Done

Created under $HERE/$OUT:
  root_ca/ca.crt, root_ca/ca.key      the demo root CA (keep the key local)
  ca-bundle.crt                       the only thing clients need to trust
  identityserver/  secondaryidp/      .key .crt .fullchain.pem .p12
  flaskapp/  aiagent/  businessapi/   .key .crt .fullchain.pem

Next steps

1. Patch each WSO2 IS install (backs up the keystores first):
     ./apply_to_is.sh --is-path /path/to/primary-wso2is-7.2.0.x   --cert identityserver
     ./apply_to_is.sh --is-path /path/to/federated-wso2is-7.2.0.x --cert secondaryidp
   Then set [server] hostname in each deployment.toml to identityserver.test /
   secondaryidp.test and restart both.

2. Add to /etc/hosts:
     127.0.0.1 identityserver.test secondaryidp.test flaskapp.test aiagent.test businessapi.test

3. Point the Python services at the bundle instead of disabling verification:
     IS_VERIFY_TLS=$HERE/$CA_BUNDLE
     SERVICE_VERIFY_TLS=$HERE/$CA_BUNDLE

4. Serve the three app services over TLS (gunicorn):
     webapp        --certfile $OUT/flaskapp/flaskapp.fullchain.pem     --keyfile $OUT/flaskapp/flaskapp.key
     ai-agent      --certfile $OUT/aiagent/aiagent.fullchain.pem       --keyfile $OUT/aiagent/aiagent.key
     business-api  --certfile $OUT/businessapi/businessapi.fullchain.pem --keyfile $OUT/businessapi/businessapi.key
EOF
