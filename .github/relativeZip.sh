#!/bin/bash

# Fail fast
set -e

# -----------------------------------------------------------------------
# configure paths

zipDirectory="$1"
zipDestination="$2"

# -----------------------------------------------------------------------
# functions 

zipRelative() {
    local directory="$1"
    local zip="$2"

    (cd "$directory" && zip -r - .) > "$zip"
}

zipRelative "$zipDirectory" "$zipDestination"