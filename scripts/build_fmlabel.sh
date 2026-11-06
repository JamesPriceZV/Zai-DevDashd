#!/bin/bash
# SPDX-License-Identifier: MIT
# Explicit optional compilation; no model inference is performed by this script.
set -euo pipefail
SOURCE="$(cd "$(dirname "$0")/fmlabel" && pwd)"
STATE="${ZAI_DEVDASHD_STATE:-$HOME/Library/Application Support/zai-devdashd}"
BUILD="${ZAI_FMLABEL_BUILD:-$HOME/Library/Caches/zai-devdashd/fmlabel-build}"
mkdir -p "$BUILD" "$STATE/bin"
chmod 700 "$BUILD" "$STATE/bin"
swift build --package-path "$SOURCE" --scratch-path "$BUILD" -c release
install -m 700 "$BUILD/release/fmlabel" "$STATE/bin/fmlabel"
printf '%s\n' 'Optional helper installed. Model interpretation remains disabled until configured.'
