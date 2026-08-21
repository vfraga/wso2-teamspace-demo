#!/usr/bin/env bash
# Apply one generated certificate to ONE WSO2 IS 7.2.0 install:
#   - replace the "wso2carbon" keypair in repository/resources/security/wso2carbon.p12
#   - add the demo root CA to repository/resources/security/client-truststore.p12
#
# Usage:
#   ./apply_to_is.sh --is-path /path/to/wso2is-7.2.0.x --cert identityserver
#   ./apply_to_is.sh --is-path /path/to/wso2is-7.2.0.x --cert secondaryidp \
#       [--keystore-password wso2carbon] [--truststore-password wso2carbon] [--dry-run]
#
# Run it once per IS instance: --cert identityserver for the primary,
# --cert secondaryidp for the federated one. Both keystores are backed up with
# a timestamp before anything is modified.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

OUT="out"
CA_CRT="$OUT/root_ca/ca.crt"
CA_ALIAS="teamspace-demo-ca"
KEY_ALIAS="wso2carbon"

# Set by generate.sh when it exports the PKCS#12 bundles.
P12_PASSWORD="wso2carbon"

IS_PATH=""
CERT_NAME=""
KEYSTORE_PASSWORD="wso2carbon"
TRUSTSTORE_PASSWORD="wso2carbon"
DRY_RUN=0

usage() {
    # BSD sed has no \? in BREs, so strip the marker and one space separately.
    sed -n '2,13p' "$0" | sed -e 's/^#//' -e 's/^ //'
    exit 0
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

need_arg() {
    [ $# -ge 2 ] && [ -n "$2" ] || die "$1 requires a value."
}

while [ $# -gt 0 ]; do
    case "$1" in
        --is-path)             need_arg "$1" "${2:-}"; IS_PATH="$2"; shift ;;
        --cert)                need_arg "$1" "${2:-}"; CERT_NAME="$2"; shift ;;
        --keystore-password)   need_arg "$1" "${2:-}"; KEYSTORE_PASSWORD="$2"; shift ;;
        --truststore-password) need_arg "$1" "${2:-}"; TRUSTSTORE_PASSWORD="$2"; shift ;;
        --dry-run)             DRY_RUN=1 ;;
        -h|--help)             usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
    shift
done

[ -n "$IS_PATH" ]   || die "--is-path is required (the wso2is-7.2.0.x directory)."
[ -n "$CERT_NAME" ] || die "--cert is required (identityserver for the primary IS, secondaryidp for the federated one)."

command -v keytool >/dev/null 2>&1 || \
    die "keytool not found on PATH. It ships with the JDK; try 'export PATH=\$JAVA_HOME/bin:\$PATH'."

# --- validate inputs before touching the user's install ---------------------
P12="$OUT/$CERT_NAME/$CERT_NAME.p12"
if [ ! -f "$P12" ]; then
    echo "ERROR: $HERE/$P12 not found." >&2
    if [ ! -d "$OUT" ]; then
        echo "       The PKI has not been generated yet. Run ./generate.sh first." >&2
    else
        echo "       Only the two WSO2 IS certificates get a PKCS#12 bundle." >&2
        echo "       Valid --cert values: identityserver, secondaryidp" >&2
    fi
    exit 1
fi

[ -d "$IS_PATH" ] || die "--is-path '$IS_PATH' is not a directory."
SECURITY_DIR="$IS_PATH/repository/resources/security"
[ -d "$SECURITY_DIR" ] || \
    die "'$IS_PATH' does not look like a WSO2 IS install: $SECURITY_DIR is missing."

# IS 7.2.0 ships PKCS#12 stores. -storetype is passed explicitly on every
# keytool call: it infers the type from the file otherwise, and guesses wrong
# often enough to matter.
KEYSTORE_TYPE="PKCS12"
TRUSTSTORE_TYPE="PKCS12"

KEYSTORE="$SECURITY_DIR/wso2carbon.p12"
TRUSTSTORE="$SECURITY_DIR/client-truststore.p12"
[ -f "$KEYSTORE" ]   || die "$KEYSTORE not found (this project targets WSO2 IS 7.2.0)."
[ -f "$TRUSTSTORE" ] || die "$TRUSTSTORE not found (this project targets WSO2 IS 7.2.0)."

STAMP="$(date +%Y%m%d-%H%M%S)"

echo "Applying demo certificate to WSO2 IS"
echo "  is-path:    $IS_PATH"
echo "  cert:       $CERT_NAME ($P12)"
echo "  keystore:   $KEYSTORE ($KEYSTORE_TYPE)"
echo "  truststore: $TRUSTSTORE ($TRUSTSTORE_TYPE)"
echo "  dry-run:    $DRY_RUN"
echo

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN: $*"
        return 0
    fi
    "$@"
}

# An absent alias is the NORMAL state the first time this runs against a stock
# install, so check before deleting rather than deleting and ignoring the
# error. keytool prints "keytool error: ... Alias <x> does not exist" on
# STDOUT, so suppressing stderr does not hide it -- blind-deleting made a
# successful first run look like it had failed.
alias_exists() {
    keytool -list -alias "$1" -keystore "$2" -storetype "$3" -storepass "$4" \
        >/dev/null 2>&1
}

delete_alias_if_present() {
    local alias="$1" store="$2" type="$3" pass="$4"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY-RUN: keytool -delete -alias $alias -keystore $store -storetype $type   # only if present"
        return 0
    fi
    if alias_exists "$alias" "$store" "$type" "$pass"; then
        keytool -delete -alias "$alias" -keystore "$store" -storetype "$type" -storepass "$pass"
        echo "  removed existing alias '$alias'"
    else
        echo "  no existing alias '$alias'"
    fi
}

# --- a. backups first -------------------------------------------------------
# These are the user's real WSO2 installs, not something this repo owns.
echo "==> Backups"
run cp "$KEYSTORE" "$KEYSTORE.bak.$STAMP"
run cp "$TRUSTSTORE" "$TRUSTSTORE.bak.$STAMP"
echo "  $KEYSTORE.bak.$STAMP"
echo "  $TRUSTSTORE.bak.$STAMP"
echo

# --- b. replace the wso2carbon keypair --------------------------------------
# IS looks the key up by alias, so the new keypair has to land under exactly
# "wso2carbon" — the old entry must go first, keytool will not overwrite it.
echo "==> Keystore: $KEYSTORE"
delete_alias_if_present "$KEY_ALIAS" "$KEYSTORE" "$KEYSTORE_TYPE" "$KEYSTORE_PASSWORD"

# PKCS#12 has no separate per-key password, so -destkeypass must equal
# -deststorepass; keytool warns and overrides it otherwise.
run keytool -importkeystore \
    -srckeystore "$P12" \
    -srcstoretype PKCS12 \
    -srcstorepass "$P12_PASSWORD" \
    -srcalias "$KEY_ALIAS" \
    -destkeystore "$KEYSTORE" \
    -deststoretype "$KEYSTORE_TYPE" \
    -deststorepass "$KEYSTORE_PASSWORD" \
    -destkeypass "$KEYSTORE_PASSWORD" \
    -destalias "$KEY_ALIAS" \
    -noprompt
echo "  imported keypair + chain as alias '$KEY_ALIAS'"
echo

# --- c. trust the demo root CA ----------------------------------------------
# Both IS instances are issued certificates by the SAME root CA, so importing
# that one certificate here is what makes the primary trust the federated IS
# (and vice versa) during the federated login hops. That mutual trust is the
# entire reason this demo uses a CA instead of a self-signed cert per service:
# with self-signed certs every party would need every other party's leaf.
echo "==> Truststore: $TRUSTSTORE"
delete_alias_if_present "$CA_ALIAS" "$TRUSTSTORE" "$TRUSTSTORE_TYPE" "$TRUSTSTORE_PASSWORD"

run keytool -importcert \
    -alias "$CA_ALIAS" \
    -file "$CA_CRT" \
    -keystore "$TRUSTSTORE" \
    -storetype "$TRUSTSTORE_TYPE" \
    -storepass "$TRUSTSTORE_PASSWORD" \
    -trustcacerts \
    -noprompt
echo "  imported $CA_CRT as alias '$CA_ALIAS'"
echo

if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run only; nothing was modified."
    exit 0
fi

EXPECTED_HOST="$CERT_NAME.test"
cat <<EOF
Done.

Before restarting, check repository/conf/deployment.toml:

  [server]
  hostname = "$EXPECTED_HOST"

That is the name IS stamps into the URLs it advertises (OIDC discovery, iss,
jwks_uri), and clients verify it against the certificate's SAN. Use
identityserver.test for the primary IS and secondaryidp.test for the
federated one.

IS still calls some of its own APIs over localhost regardless of that setting,
which is why every generated certificate keeps localhost and 127.0.0.1 in its
SAN alongside the .test name -- those internal calls keep working.

Then restart this IS (wso2server.sh) for the new keystore to take effect.
EOF
