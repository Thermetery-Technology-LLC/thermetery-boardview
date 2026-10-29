# Thermetery Boardview — Android shell

Single-activity Android app: a fullscreen WebView hosts the JS renderer
(`app/src/main/assets/viewer.html`), Chaquopy embeds CPython 3.13 with the
board parsers, and prebuilt native kernels live in
`app/src/main/jniLibs/<abi>/`. The binding contract between the three
components is `../docs/android_contract.md` — read it before changing
anything here.

## Layout / ownership

| Path | What | Owner |
|---|---|---|
| `settings.gradle.kts`, `build.gradle.kts`, `gradle.properties`, `app/build.gradle.kts` | Gradle config (AGP 8.7.3, Kotlin 2.0.21, Chaquopy 17.0.0) | shell |
| `app/src/main/java/com/thermetery/boardview/` | Kotlin sources | shell |
| `app/src/main/res/` | theme + launcher icon | shell |
| `app/src/main/assets/` | viewer.html / viewer.js / viewer.css (architecture notes: `../docs/WEB_CORE.md`) | renderer agent |
| `app/src/main/jniLibs/` | prebuilt `lib{tvw,xzz,rc6}_native.so` (arm64-v8a, x86_64) | already built — do not touch |

Python sources are **not** checked in under `android/` — a Gradle task
(`:app:stagePythonSources`) copies the curated module list from the repo
root into `app/build/staged-python/` before Chaquopy packages them,
preserving the `src/` package paths (contract §6). The authoritative
list is `pythonRootModules` + `pythonPackageModules` in
`app/build.gradle.kts` (`board_export.py` plus the `src/` parser core,
19 files today). If the build fails with `stagePythonSources: missing
module(s)`, the named file does not exist at the repo root — supply the
module or update the staging list *together with* the code change that
moved it.

## Build

Requirements (nothing machine-specific is hardcoded in the Gradle files):

| Need | How it is found |
|---|---|
| JDK 17 | `JAVA_HOME` (or a JDK 17 `java` on `PATH`) |
| Gradle 8.10 | no wrapper is checked in — invoke a local Gradle install directly with `-p android` |
| Android SDK (build-tools 35+, platform android-35; AGP downloads the platform if missing) | `sdk.dir=<path-to-sdk>` in the gitignored `android/local.properties` (e.g. `sdk.dir=F:/Android_SDK` or `sdk.dir=C\:\\Android\\sdk`) takes precedence; when that file is absent AGP falls back to the `ANDROID_HOME` environment variable. |
| CPython **3.13** for Chaquopy's `buildPython` | see below |

Chaquopy 17 requires `buildPython` to be the **same minor version** as the
target (`version = "3.13"` in `app/build.gradle.kts`), so an older interpreter
(e.g. an anaconda 3.11) does not work. The `chaquopy { }` block in
`app/build.gradle.kts` resolves the interpreter in this order:

1. `CHAQUOPY_BUILD_PYTHON` env var — an explicit path to a 3.13 interpreter.
   Set this when the launcher below cannot find one.
2. On Windows: the Python launcher, `py -3.13` (any CPython 3.13 installed
   via the Python install manager, `py install 3.13`, or python.org).
3. Elsewhere: `python3.13` on `PATH`.

```powershell
$env:JAVA_HOME    = "C:/Android/jdk-17.0.20.1+1"       # a JDK 17
$env:ANDROID_HOME = "F:/Android_SDK"                   # fallback when android/local.properties has no sdk.dir
# $env:CHAQUOPY_BUILD_PYTHON = "C:/path/to/python3.13/python.exe"   # only if `py -3.13` is unavailable

C:/Android/gradle-8.10.2/bin/gradle.bat -p android :app:assembleDebug --console=plain
```

- First build downloads AGP/Chaquopy from Google/Maven Central and
  `numpy==1.26.2` wheels from the Chaquopy pip repository — allow several
  minutes.
- `:app:stagePythonSources` runs before every Python-touching task (see
  "Layout / ownership" above).

The debug APK lands at:

```
android/app/build/outputs/apk/debug/app-debug.apk
```

## Run on the emulator

Any x86_64 AVD (API 24+) works; the emulator needs a hypervisor on the host
(Windows: AEHD driver, Hyper-V or Windows Hypervisor Platform — without one
`emulator.exe` exits with "x86_64 emulation currently requires hardware
acceleration").

```powershell
$sdk = "F:/Android_SDK"
& "$sdk/emulator/emulator.exe" -list-avds
& "$sdk/emulator/emulator.exe" -avd <avd-name>          # leave running
& "$sdk/platform-tools/adb.exe" install -r android/app/build/outputs/apk/debug/app-debug.apk
& "$sdk/platform-tools/adb.exe" shell am start -n com.thermetery.boardview/.MainActivity
```

Useful logcat tags:

```powershell
& "$sdk/platform-tools/adb.exe" logcat -s Boardview BoardviewPy BoardviewJS python.stdout python.stderr
```

Exercising "open with" from the shell (push a sample board to
`/sdcard/Download/` first):

```powershell
# name-in-path route (manifest filter 1)
adb shell am start -a android.intent.action.VIEW -t application/octet-stream --grant-read-uri-permission -d "content://com.android.externalstorage.documents/document/primary%3ADownload%2Fboard.tvw"
# opaque-id route (manifest filter 2); find <id> with:
#   adb shell content query --uri content://media/external/downloads --projection _id:_display_name
adb shell am start -a android.intent.action.VIEW -t application/octet-stream --grant-read-uri-permission -d "content://com.android.providers.downloads.documents/document/msf%3A<id>"
# which activities the system offers for an opaque-id octet-stream document:
adb shell pm query-activities -a android.intent.action.VIEW -t application/octet-stream -d content://com.android.providers.downloads.documents/document/msf%3A1
```

- `BoardviewPy` — Python startup + the `board_export.ping()` native-kernel
  availability JSON (logged once at app start).
- `BoardviewJS` — `Android.log(...)` passthrough and WebView console output.
- `Boardview` — shell-side events (errors forwarded to the renderer, key saves).

## Behaviour notes

- Opening a board: in-app picker (SAF `ACTION_OPEN_DOCUMENT`) or "open with"
  from a file manager / downloads list / Drive (`ACTION_VIEW`). The stream
  is copied to `cacheDir/boards/<displayName>` and kept for the session —
  the lazy trace build re-reads it.
- "Open with" registration (`AndroidManifest.xml`) uses two `ACTION_VIEW`
  filters, because Android matches filters on the URI, not on the
  document's display name:
  1. **Name-pattern filter** — `file`/`content` scheme, `mimeType */*`,
     `pathPattern` globs for `.tvw .fz .pcb .cad .brd .brd2 .bv .asc`
     (with extra-dot variants, since the globs are non-greedy). Matches
     whenever the file name is in the URI path: every `file://` URI and
     `content://` URIs whose document id embeds the path (e.g.
     `com.android.externalstorage.documents/document/primary%3ADownload%2Fx.tvw`
     from "Internal storage > Download").
     Filter 1 declares a `mimeType`, so it matches only when the intent
     carries a type: the resolver infers one from the provider for
     `content://` URIs, while a `file://` sender must set it (file
     managers normally do). A type-less intent, or a provider whose
     `getType` returns null, matches neither filter; no type-less twin is
     declared because `file://` VIEW intents are legacy (apps targeting
     API 24+ cannot expose them).
  2. **Opaque-id filter** — `content` scheme + `mimeType
     application/octet-stream`, no host/pathPattern. The Downloads root
     (`com.android.providers.downloads.documents/document/msf%3A<id>`),
     Google Drive, Chrome/Discord/Telegram downloads and many file managers
     expose opaque document ids, so no pathPattern can match. Extensions
     Android has no MIME mapping for (`.tvw .fz .pcb .cad .brd .brd2
     .bv`) are typed `application/octet-stream`, and this filter makes
     Boardview appear in the chooser for them. Known gaps: `.asc` has a
     `MimeTypeMap` mapping, so it is not typed octet-stream and an
     opaque-id `.asc` is not offered — moot, because a lone `.asc` only ever shows the folder hint;
     and a sender that attaches some other type (`application/zip`, a
     vendor type) is not matched either.
  Because filter 2 is offered for *any* opaque-id binary document,
  `MainActivity.handleIncomingUri` gates on the `DISPLAY_NAME` extension
  (both routes, the in-app picker included): the name is trimmed and
  trailing dots dropped; a suffix counts as an extension only when it is
  1–5 alphanumerics with at least one letter (`rev1.0`, `Z4.1`,
  `820-1234_rev1.01` have none). A name whose extension is not one of the
  eight above is refused with "`<name>` is not a supported boardview (.tvw
  .fz .pcb .cad .brd .brd2 .bv .asc)" before anything is copied; a name
  with **no** extension falls through to the parser's content sniffing
  (`dump.v2`-style names are still refused). The `DISPLAY_NAME`/`SIZE`
  query, the gate and the copy all run on the worker thread — Drive and
  browser-download providers can be slow — and a document larger than
  512 MiB (reported `SIZE`, or a bounded copy as backstop) is refused
  before it can fill the cache.
  **"Always" caveat:** since the chooser lists Boardview for every
  opaque-id binary, a user who picks Boardview with "Always" for, say, a
  `.bin` makes it the default for all such files (each then only shows the
  error above). Clear it under Settings > Apps > Boardview > Open by
  default > Clear defaults.
- Folder-shaped boards (eM-Test Expert `.asc` sets): the "Folder" toolbar
  button opens the SAF tree picker; the shell copies the folder's `*.asc`
  members into `cacheDir/boards/<folderName>/` and parses the directory.
  Opening a single `.asc` file shows a hint that routes to the folder
  picker (a single-document grant cannot read the sibling members).
- Encrypted boards (`.fz`, XZZ `.pcb`): a native dialog prompts for the key,
  up to 3 attempts, mirroring `viewer.py:_load_with_key_prompt`. Checking
  "Remember on this device" stores the working key at
  `filesDir/keys/<format>.txt`; it is supplied automatically next time.
  `android:allowBackup="false"` keeps keys out of cloud backups. To forget
  keys: Settings → Apps → Boardview → Clear storage (or delete the file via
  `adb shell run-as com.thermetery.boardview rm files/keys/<format>.txt`).
- Renderer-without-Android development (desktop browser) is covered by the
  contract's dev-harness rule and needs nothing from this shell.
