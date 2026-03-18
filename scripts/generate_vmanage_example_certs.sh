#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  generate_vmanage_example_certs.sh \
    --output-dir <dir> \
    --org <organization> \
    --root-cn <root common name> \
    --server-cn <server common name> \
    --valid-days <days>
EOF
}

output_dir=""
org=""
root_cn=""
server_cn=""
valid_days=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      output_dir="${2:-}"
      shift 2
      ;;
    --org)
      org="${2:-}"
      shift 2
      ;;
    --root-cn)
      root_cn="${2:-}"
      shift 2
      ;;
    --server-cn)
      server_cn="${2:-}"
      shift 2
      ;;
    --valid-days)
      valid_days="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$output_dir" || -z "$org" || -z "$root_cn" || -z "$server_cn" || -z "$valid_days" ]]; then
  usage >&2
  exit 1
fi

mkdir -p "$output_dir"

manifest_path="$output_dir/.manifest"
desired_manifest="$(cat <<EOF
ORG=$org
ROOT_CN=$root_cn
SERVER_CN=$server_cn
VALID_DAYS=$valid_days
EOF
)"

required_files=(
  "$output_dir/root-ca.key"
  "$output_dir/root-ca.crt"
  "$output_dir/server.key"
  "$output_dir/server.csr"
  "$output_dir/server.crt"
)

all_present=true
for file in "${required_files[@]}"; do
  if [[ ! -s "$file" ]]; then
    all_present=false
    break
  fi
done

if [[ "$all_present" == true && -f "$manifest_path" ]]; then
  if diff -q "$manifest_path" <(printf '%s\n' "$desired_manifest") >/dev/null; then
    exit 0
  fi
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

cat >"$tmpdir/root-ca.cnf" <<EOF
[req]
default_bits = 4096
distinguished_name = dn
x509_extensions = v3_ca
prompt = no

[dn]
O = $org
CN = $root_cn

[v3_ca]
basicConstraints = critical,CA:true
keyUsage = critical,keyCertSign,cRLSign,digitalSignature
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
EOF

cat >"$tmpdir/server.cnf" <<EOF
[req]
default_bits = 2048
distinguished_name = dn
req_extensions = v3_req
prompt = no

[dn]
O = $org
CN = $server_cn

[v3_req]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth,clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $server_cn
EOF

openssl genrsa -out "$tmpdir/root-ca.key" 4096
openssl req -x509 -new -sha256 -key "$tmpdir/root-ca.key" -days "$valid_days" -out "$tmpdir/root-ca.crt" -config "$tmpdir/root-ca.cnf"

openssl genrsa -out "$tmpdir/server.key" 2048
openssl req -new -key "$tmpdir/server.key" -out "$tmpdir/server.csr" -config "$tmpdir/server.cnf"
openssl x509 -req -sha256 -days "$valid_days" \
  -in "$tmpdir/server.csr" \
  -CA "$tmpdir/root-ca.crt" \
  -CAkey "$tmpdir/root-ca.key" \
  -CAcreateserial \
  -out "$tmpdir/server.crt" \
  -extfile "$tmpdir/server.cnf" \
  -extensions v3_req

install -m 600 "$tmpdir/root-ca.key" "$output_dir/root-ca.key"
install -m 644 "$tmpdir/root-ca.crt" "$output_dir/root-ca.crt"
install -m 600 "$tmpdir/server.key" "$output_dir/server.key"
install -m 644 "$tmpdir/server.csr" "$output_dir/server.csr"
install -m 644 "$tmpdir/server.crt" "$output_dir/server.crt"
printf '%s\n' "$desired_manifest" >"$manifest_path"
