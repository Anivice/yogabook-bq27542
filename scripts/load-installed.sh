#!/usr/bin/env bash
set -euo pipefail
runtime=/var/lib/yogabook-bq27542/modules/$(uname -r)
[[ -r $runtime/scripts/load.sh ]] || {
    echo "No installed BQ27542 modules for $(uname -r); build and run scripts/install-services.sh." >&2
    exit 1
}
exec /usr/bin/bash "$runtime/scripts/load.sh"
