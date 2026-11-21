#!/bin/sh
set -eu

setup() {
    database="$1"
    directory="$2"
    sql() {
        temporal-sql-tool --plugin postgres12 --ep temporal-postgres -u temporal -p 5432 --db "$database" "$@"
    }
    sql setup-schema -v 0.0
    sql update-schema -d "/etc/temporal/schema/postgresql/v12/$directory/versioned"
}

setup temporal temporal
setup temporal_visibility visibility
