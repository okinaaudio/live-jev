# Release Live Jev

## Prerequisites

Release on an Apple Silicon Mac with Xcode command-line tools and a valid Developer ID Application certificate. Set `NOTARY_PROFILE` to a keychain profile created for `xcrun notarytool`. To use a wrapper instead, set `NOTARIZE_TOOL` to its executable path. The wrapper must accept the DMG path and print `status: Accepted` on success.

The release script downloads the pinned CPython 3.12 archive from python-build-standalone. It verifies the archive against the release's published `SHA256SUMS` entry before use.

## Create a release

Run this command from the repository root:

```sh
scripts/release.sh --version 1.02
```

The script selects the first Developer ID Application identity. Pass `--identity "Developer ID Application: ..."` to select one explicitly. Use `--skip-notarize` only for a local packaging test.

The script builds a fresh release executable, creates the icon and copied `Info.plist`, bundles a pruned CPython runtime, and copies the daemon and Remote Script files. It tests the daemon before and after signing. The second test catches hardened-runtime failures that cannot appear in the unsigned bundle.

The script then scans `Contents/` for private build paths, signs the disk image, submits it for notarization, validates the staple, and asks Gatekeeper to assess both the disk image and its app. The final summary prints the disk image path, size, SHA-256, signing authority, and notarization status.

`scripts/build-app.sh` builds `DaemonClient.swift` directly from the checkout. Swift's `#filePath` therefore bakes the checkout location into the development executable. `scripts/release.sh` copies the Swift package to its work directory and replaces that fallback with an empty string in the copied source only. The release app then resolves the bundled daemon. The development source and development build behavior do not change.

## Python entitlement fallback

The release starts with no entitlements. If the notarized app's bundled Python cannot load its own `.so` files, add `com.apple.security.cs.disable-library-validation` to the Python executable's signature and repeat signing, smoke testing, packaging, and notarization. Do not add unrelated entitlements.

Anyone who inspects the signature can see the Developer ID certificate holder's legal name.
