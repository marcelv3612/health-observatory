#!/bin/sh
set -eu
companionRoot=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
companionCompiler=$(xcrun --find swiftc)
companionToolchainBin=$(dirname -- "$companionCompiler")
export CLANG_MODULE_CACHE_PATH="$companionRoot/.cache/modules"
export SWIFT_MODULECACHE_PATH="$companionRoot/.cache/modules"
# Some Command Line Tools releases omit automatic TestingMacros discovery.
for companionPlugin in \
    "$companionToolchainBin/../lib/swift/host/plugins/testing/libTestingMacros.dylib" \
    "$companionToolchainBin/../lib/swift/host/plugins/libTestingMacros.dylib"; do
    if [ -f "$companionPlugin" ]; then
        set -- "$@" -Xswiftc -load-plugin-library -Xswiftc "$companionPlugin"
        break
    fi
done
swift test --package-path "$companionRoot" \
    --scratch-path "$companionRoot/.build" \
    --cache-path "$companionRoot/.cache/cache" \
    --config-path "$companionRoot/.cache/config" \
    --security-path "$companionRoot/.cache/security" \
    --manifest-cache local --disable-sandbox --disable-keychain --disable-netrc \
    --disable-dependency-cache --disable-index-store -j 4 \
    -Xswiftc -module-cache-path -Xswiftc "$companionRoot/.cache/modules" "$@"
