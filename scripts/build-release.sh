#!/usr/bin/env bash
# build-release.sh — the ONE build where the remote console is compiled out.
#
# the publishing conventions §2: IPAs published to GitHub/SideStore have the bridge
# compiled out; every other build keeps it on, because OTA test builds vastly
# outnumber public releases. The bridge on :8771 is an UNAUTHENTICATED TCP
# command server (CVar writes, input, crash.txt/log reads) and a tapped
# `paperboat://console` link can switch it on — so a public build must not
# merely decline to listen, it must contain no listener at all.
#
# This script forces PAPERBOAT_REMOTE_CONSOLE=OFF and then ASSERTS on the built
# binary that the bridge is genuinely gone rather than trusting the flag; a
# mis-wired #if is exactly the failure worth catching. It also asserts that
# symbols survived (crash.txt walks the frame-pointer chain and symbolicates
# from the binary's own symbol table — a stripped Release binary makes every
# shipped crash report address-only, silently) and that the shipped version
# string matches VERSION (SideStore compares that string).
#
# Output: release/paperboat-<version>-iOS.ipa
#
# Two routes to the IPA, chosen by whether signing is on:
#   * signed (default)  — xcodebuild archive + -exportArchive, the sibling route
#                         (Lighthouse scripts/build-release.sh). This needs
#                         overlay 0018 (INSTALL_PATH/SKIP_INSTALL on the bundle
#                         target): without it the archive is EMPTY and the
#                         export dies with the misleading "expected one {} but
#                         found debugging" (the design notes D21/D22, M-021b).
#   * unsigned          — Payload/ zip of the built .app, which is upstream's own
#                         CI route and is what a pre-device-gate proof needs.
#     PAPERBOAT_IOS_SIGNING=OFF scripts/build-release.sh
#
# Env knobs: PAPERBOAT_IOS_SIGNING (ON|OFF), PAPERBOAT_IOS_TEAM,
#            PAPERBOAT_ASC_KEY_ID / PAPERBOAT_ASC_ISSUER_ID / PAPERBOAT_ASC_KEY_PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build-ios"
SIGNING="${PAPERBOAT_IOS_SIGNING:-ON}"
TEAM="${PAPERBOAT_IOS_TEAM:?set your Apple Developer team id}"

die() { printf '\033[31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[ -n "$VERSION" ] || die "VERSION file is empty"

info "RELEASE build — console OFF, signing $SIGNING, v$VERSION"
PAPERBOAT_REMOTE_CONSOLE=OFF PAPERBOAT_IOS_SIGNING="$SIGNING" "$ROOT/scripts/build-ios.sh"

APP="$BUILD/Release-iphoneos/Paperboat.app"
[ -d "$APP" ] || die "expected app at $APP"

mkdir -p "$ROOT/release"
STAGE="$BUILD/export-release"
rm -rf "$STAGE"
mkdir -p "$STAGE"

if [ "$SIGNING" = "ON" ]; then
    # The App Store Connect key is OPTIONAL: its only job is to let
    # -allowProvisioningUpdates CREATE or REFRESH a profile. This Mac carries a
    # cached wildcard team profile (to 2027-07-17), so the archive
    # and export sign without ever talking to Apple. Set the trio when a profile
    # genuinely needs refreshing.
    KEY_ID="${PAPERBOAT_ASC_KEY_ID:-}"
    ISSUER_ID="${PAPERBOAT_ASC_ISSUER_ID:-}"
    AUTH=(-allowProvisioningUpdates)
    if [ -n "$KEY_ID" ] && [ -n "$ISSUER_ID" ]; then
        KEY_PATH="${PAPERBOAT_ASC_KEY_PATH:-$HOME/.appstoreconnect/private_keys/AuthKey_${KEY_ID}.p8}"
        [ -f "$KEY_PATH" ] || die "App Store Connect key missing at $KEY_PATH"
        AUTH+=(-authenticationKeyPath "$KEY_PATH"
               -authenticationKeyID "$KEY_ID"
               -authenticationKeyIssuerID "$ISSUER_ID")
        info "signing with App Store Connect key $KEY_ID (profiles may be refreshed)"
    else
        info "no ASC key in the environment — signing with the CACHED team profile"
    fi

    info "archive"
    # STRIP_INSTALLED_PRODUCT / COPY_PHASE_STRIP: the archive action is an
    # INSTALL-style build, and once overlay 0018 makes the bundle target
    # installable Xcode strips the installed product by default — the exported
    # IPA came back with 913 symbols instead of 72,578, which would make every
    # shipped crash.txt address-only (assertion 2 below is exactly that check,
    # and it caught this). Turning both off restores the full symbol table.
    xcodebuild -project "$BUILD/Paperboat.xcodeproj" -scheme Paperboat \
        -configuration Release -destination 'generic/platform=iOS' \
        archive -archivePath "$BUILD/PaperboatRelease.xcarchive" \
        STRIP_INSTALLED_PRODUCT=NO COPY_PHASE_STRIP=NO \
        "${AUTH[@]}" | tail -3

    # Loud, because the failure mode downstream names the wrong thing: an
    # archive with no app in it makes -exportArchive complain about the
    # "method" key (D21). Check the two things overlay 0018 exists to produce.
    ARCHAPP="$BUILD/PaperboatRelease.xcarchive/Products/Applications/Paperboat.app"
    [ -d "$ARCHAPP" ] || die "archive has no Products/Applications/Paperboat.app — is overlay 0018 applied?"
    /usr/libexec/PlistBuddy -c 'Print :ApplicationProperties' \
        "$BUILD/PaperboatRelease.xcarchive/Info.plist" > /dev/null 2>&1 \
        || die "archive Info.plist has no ApplicationProperties — is overlay 0018 applied?"
    info "    archive: Products/Applications/Paperboat.app + ApplicationProperties present"

    info "export IPA"
    cat > "$BUILD/export-release.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key><string>debugging</string>
	<key>teamID</key><string>$TEAM</string>
	<key>signingStyle</key><string>automatic</string>
	<key>stripSwiftSymbols</key><false/>
	<key>compileBitcode</key><false/>
</dict>
</plist>
PLIST
    xcodebuild -exportArchive -archivePath "$BUILD/PaperboatRelease.xcarchive" \
        -exportPath "$STAGE" -exportOptionsPlist "$BUILD/export-release.plist" \
        "${AUTH[@]}" | tail -3
    IPA="$(ls "$STAGE"/*.ipa 2>/dev/null | head -1)"
    [ -f "$IPA" ] || die "no IPA produced by -exportArchive"

    # SYNC WAVE 2 trap, and it bites here too: `xcodebuild archive` replaces
    # build-ios/Release-iphoneos/Paperboat.app with a symlink into
    # ArchiveIntermediates. build-ios.sh clears it at the top of every build,
    # but leaving it behind makes the next `devicectl install app` copy a
    # 181-byte symlink. Remove it here so the tree is left sane either way.
    if [ -L "$APP" ]; then rm -f "$APP"; fi
else
    # Unsigned: upstream's own CI route. Payload/<App>.app zipped; SideStore and
    # every sideloading tool re-sign it on the way in.
    info "packaging unsigned IPA (Payload/ zip — no signing identity requested)"
    rm -rf "$STAGE/Payload"
    mkdir -p "$STAGE/Payload"
    cp -R "$APP" "$STAGE/Payload/"
    IPA="$STAGE/Paperboat.ipa"
    (cd "$STAGE" && zip -qry "$(basename "$IPA")" Payload)
    [ -f "$IPA" ] || die "no IPA produced by the zip route"
fi

# ---- the assertions this script exists for, on the IPA's own binary ---------
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
unzip -q "$IPA" -d "$WORKDIR"
APPBIN="$WORKDIR/Payload/Paperboat.app/Paperboat"
IPAPLIST="$WORKDIR/Payload/Paperboat.app/Info.plist"
[ -f "$APPBIN" ] || die "no app binary inside the IPA"

# 1. the bridge is genuinely gone. Three independent markers, all inside the
#    #if PAPERBOAT_REMOTE_CONSOLE block in app/ios/PaperBoatIosShell.m: the
#    listener's log line, a verb only the bridge answers, and the safe-area
#    reply format. Any one of them surviving means the switch did not work.
for MARK in 'console bridge listening' 'crashtest' 'safearea_px'; do
    HITS="$(strings -a "$APPBIN" 2>/dev/null | grep -c "$MARK" || true)"
    [ "$HITS" = "0" ] || die "REMOTE CONSOLE IS PRESENT in the release IPA ($HITS × '$MARK'). Do not publish this binary."
done
info "    remote console: absent (3 markers checked)"

# 2. symbols survived — crash.txt symbolicates from this table at runtime.
SYMS="$(nm "$APPBIN" 2>/dev/null | wc -l | tr -d ' ')"
[ "$SYMS" -gt 10000 ] || die "only $SYMS symbols — binary looks STRIPPED; every shipped crash.txt would be address-only"
info "    symbols: $SYMS (not stripped)"

# 3. the shipped strings. SideStore compares CFBundleShortVersionString; the
#    bundle id is what an OTA manifest has to name.
PLISTV="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$IPAPLIST" 2>/dev/null || echo "")"
PLISTID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$IPAPLIST" 2>/dev/null || echo "")"
[ "$PLISTV" = "$VERSION" ] || die "IPA reports version '$PLISTV' but VERSION says '$VERSION'"
[ "$PLISTID" = "com.rebelancap.paperboat" ] || die "IPA bundle id is '$PLISTID', expected com.rebelancap.paperboat"
info "    version: $PLISTV   bundle: $PLISTID"

# 4. signature, when one was asked for.
if [ "$SIGNING" = "ON" ]; then
    codesign -dv "$WORKDIR/Payload/Paperboat.app" 2>&1 | sed -n '1,4p'
else
    info "    UNSIGNED — this IPA is a compile/packaging proof, not an installable artifact"
fi

OUT="$ROOT/release/paperboat-$VERSION-iOS.ipa"
cp "$IPA" "$OUT"
info "-> $(basename "$OUT")  ($(du -h "$OUT" | cut -f1))"
ls -la "$ROOT/release/"
