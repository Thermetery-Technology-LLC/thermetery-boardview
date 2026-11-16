# Thermetery Boardview — iOS shell

Single-screen iOS app mirroring the Android shell architecture
(`../android/README.md`): a fullscreen WKWebView hosts the **same** JS
renderer (`../android/app/src/main/assets/viewer.html` — staged into the
bundle at build time, never forked), an embedded CPython 3.13
(BeeWare Python-Apple-support) runs the **same** staged parser core
(`board_export.py` + `src/`), and the **same** three C kernels
(`src/parsers/native/{tvw,xzz,rc6}_native.c`) are cross-compiled to iOS
dylibs in a build phase. The binding contract is
`../docs/android_contract.md` — the iOS shell implements the §4/§6 shell
role; §1–§3 and §5 are shared verbatim.

## Install by sideloading

Until Boardview is on the App Store, you can install it on an iPhone or
iPad from the IPA attached to each iOS release on the
[Releases page](https://github.com/Thermetery-Technology-LLC/thermetery-boardview/releases)
(tags `v<version>-ios`, file `ThermeteryBoardviewer-<version>-ios-unsigned.ipa`).

You need:

- an iPhone or iPad on iOS / iPadOS 15.0 or later (your sideloading tool
  may need a newer iOS; check its requirements);
- a Mac or Windows PC for the sideloading tool's first setup. On Windows,
  install iTunes and iCloud from Apple's website, not the Microsoft Store
  versions;
- an Apple ID. A free one works; the app needs no special entitlements.

The IPA is **unsigned on purpose**. It contains no provisioning profile and
no code signatures; your sideloading tool signs it with your own Apple ID
as it installs. It cannot be installed as-is by opening it on the device,
with Apple Configurator, or with Xcode.

1. **Download and check.** Download the `.ipa` and `SHA256SUMS-ios.txt`
   from the release into the same folder, then check the file:

   ```bash
   shasum -a 256 -c SHA256SUMS-ios.txt                     # macOS / Linux
   ```
   ```powershell
   Get-FileHash .\ThermeteryBoardviewer-<version>-ios-unsigned.ipa   # Windows
   ```

   On Windows, compare the printed hash with the one in
   `SHA256SUMS-ios.txt`; PowerShell prints it in upper case.

2. **Install it with one of these tools**, following that tool's own
   setup guide:

   - [Sideloadly](https://sideloadly.io) (macOS / Windows): connect the
     device by USB, drop the IPA into Sideloadly, enter your Apple ID and
     click Start.
   - [AltStore](https://altstore.io) (AltServer on macOS / Windows, AltStore
     on the device): copy the IPA to the device (AirDrop or Files), then in
     AltStore open **My Apps**, tap **+** and pick the IPA.
   - [SideStore](https://sidestore.io): its setup is more involved (a
     pairing file and a VPN app); follow its guide, then add the IPA from
     **My Apps → +** the same way as AltStore.

3. **Turn on Developer Mode** (iOS / iPadOS 16 and later): Settings →
   Privacy & Security → Developer Mode → On. After the restart, tap
   **Turn On** and enter your passcode. The switch only appears once an app
   signed with a development certificate (such as a sideloaded one) has
   been installed; AltStore and SideStore users usually do this while
   setting up the tool itself.

4. **Trust your Apple ID** if iOS says *Untrusted Developer* the first
   time you open the app: Settings → General → VPN & Device Management →
   your Apple ID → Trust. If you already trusted it for AltStore or
   SideStore, you will not see this.

5. **Open a board** with the in-app **Open** / **Folder** buttons, or from
   the Files app with Share → Boardview. Encrypted ASUS `.fz` and XZZPCB
   `.pcb` boards need a key, which is not shipped: the app asks for it on
   open, and **Keys** manages remembered keys (see the top-level README).

Limits that come with signing under a **free** Apple ID (they are Apple's,
not Boardview's):

- The signature lasts **7 days**. After that the app will not launch until
  it is refreshed. AltStore and SideStore refresh it in the background
  (AltStore needs AltServer running on your computer on the same Wi-Fi);
  Sideloadly can auto-refresh while your computer is on, or you can
  install it again. A paid Apple Developer account's signature lasts a
  year.
- Only **3** sideloaded apps can be active at once, and AltStore / SideStore
  count themselves as one.
- To update, install the newer IPA with the same tool and Apple ID.

Sideloading tools may change the app's bundle identifier (AltStore and
SideStore add a suffix), so a sideloaded copy and an App Store copy are
separate apps with separate data and keys.

## Layout / ownership

| Path | What | Owner |
|---|---|---|
| `project.yml` | XcodeGen project definition (team `TBF6XLG285`, app id `com.thermetery.boardview`) | shell |
| `Boardview/Sources/` | Swift shell + `PyShim.c` (CPython C-API bridge) | shell |
| `Boardview/Resources/` | app icon + launch color | shell |
| `fetch_deps.sh` | downloads Python.xcframework + numpy wheels into `vendor/` (gitignored) | shell |
| `stage_python.sh` | build phase: stages the authoritative Python module list (mirrors `:app:stagePythonSources`) | shell |
| `stage_assets.sh` | build phase: copies the shared renderer assets into the bundle at `web/` | renderer files owned by the renderer agent — copy only |
| `build_kernels.sh` | build phase: clang-compiles the three kernels → `Frameworks/<name>.dylib`, code-signed | shell |

## How the pieces bind

- **JS bridge** — viewer.js talks to `window.Android`; a `WKUserScript`
  injected at document start defines that object and forwards each call to a
  `WKScriptMessageHandler` (`bridge`), so the renderer runs unmodified and
  its desktop dev-harness never activates. Shell → renderer calls go through
  `evaluateJavaScript` with the Android quoting rules preserved: raw JSON
  object literals for `bv.onBoard`/`bv.onTraces`, JSON-escaped string
  literals for `bv.onStatus`/`bv.onError`, queued until page load.
- **Python** — started once at launch on the single serial `python-worker`
  queue (GIL + the `board_export` single-board global demand serialization),
  then `board_export.ping()` is logged (subsystem `com.thermetery.boardview`,
  category `BoardviewPy`) to prove all three native kernels loaded. Before
  init the shell exports `BOARDVIEWER_DATA_DIR` (sandboxed Application
  Support dir — python-side config/keys land there) and
  `BOARDVIEW_NATIVE_DIR` (the bundle's `Frameworks/` dir — first candidate
  in `runtime_paths.native_lib_candidates`). `runtime_paths.native_lib_names`
  maps `sys.platform == "ios"` to `.dylib` (added with this port).
- **Board files** — the picker (`UIDocumentPickerViewController`, any type —
  the parser decides, like SAF `*/*`) and "open with" copies land in
  `Caches/boards/<displayName>` and are kept for the session (lazy topology
  re-reads them; the `.topocache.pkl` sidecar lands next to the copy). The
  `.asc` folder flow mirrors Android: folder picker, `*.asc` members only,
  64-file / 32 MiB caps, fresh copy per pick, single-`.asc` picks get the
  "Pick the board folder" hint.
- **Keys** — `key_required` triggers the native dialog (3 attempts, verbatim
  Android strings). Remembering uses `Application Support/keys/<fmt>.txt`,
  excluded from backup (`allowBackup="false"` parity). One platform
  adaptation: Android's "Remember on this device" checkbox is an
  "Open and Remember" alert action here (UIAlertController has no checkbox).
  The key manager screen (validate/save/clear per format, shared
  `board_export.validate_key` semantics) and the licenses screen are ports
  of their Android activities.

## Build

Requires Xcode (26.x tested), [XcodeGen](https://github.com/yonaskolb/XcodeGen)
(`brew install xcodegen`), and a signing identity for team `TBF6XLG285`.

```bash
./fetch_deps.sh        # once: Python.xcframework + numpy 1.26.2 iOS wheels
xcodegen generate      # project.yml → Boardview.xcodeproj
xcodebuild -project Boardview.xcodeproj -scheme Boardview \
    -destination generic/platform=iOS -allowProvisioningUpdates build
```

The build phases stage everything into the bundle: `app/` (parser core),
`app_packages/` (numpy for the platform), `web/` (renderer),
`python/` (stdlib, binary modules converted to signed frameworks by
Python-Apple-support's `install_python` helper), and
`Frameworks/{tvw,xzz,rc6}_native.dylib`.

## Run on a device

```bash
xcrun devicectl list devices
xcrun devicectl device install app --device <udid> <path-to>/Boardview.app
xcrun devicectl device process launch --device <udid> com.thermetery.boardview
```

Useful log streams (Console.app or `log stream` while the device is
connected), mirroring the Android logcat tags:

- `BoardviewPy` — Python startup + the `board_export.ping()` kernel
  availability JSON (logged once at app start).
- `BoardviewJS` — `Android.log(...)` passthrough and WebView console output.
- `Boardview` — shell-side events (errors forwarded to the renderer).

## Behaviour notes

- Encrypted boards (`.fz`, XZZ `.pcb`) prompt with the same texts and retry
  semantics as Android/`viewer.py:_load_with_key_prompt`.
- `UIFileSharingEnabled` is on: boards can also be dropped into the app's
  Documents folder via Finder/Files, then opened with "Open with Boardview"
  from the Files app (the in-app picker reads anywhere).
- Renderer-without-shell development (desktop browser) is unchanged — see
  the contract's dev-harness rule; nothing here is needed for it.
