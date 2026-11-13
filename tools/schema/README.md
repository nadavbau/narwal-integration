# Regenerating the protocol schema

`custom_components/narwal/narwal_client/schema.py` is generated from the
message definitions compiled into the official Narwal Android app. Do this
when Narwal ships firmware or an app with new messages or fields.

The app is Flutter: its logic, including the generated protobuf classes,
is AOT-compiled Dart in `libapp.so`. The class names and field names survive
compilation; [blutter](https://github.com/worawit/blutter) recovers the
field numbers and types from the code that registers each field.

## Steps

1. **Get the app bundle** (`com.narwal.pita_global`, e.g. from APKMirror) and
   unzip the arm64 split's `lib/arm64-v8a/` (`libapp.so`, `libflutter.so`).

2. **Run blutter** on that folder:

   ```bash
   python3 blutter.py path/to/lib/arm64-v8a blutter_out
   ```

   It detects the Dart version (3.9.2 for app 2.11.90), builds a matching Dart
   VM and writes `blutter_out/asm/**.dart` and `blutter_out/objs.txt`. It needs
   cmake, ninja, capstone and ICU. On an Intel Mac without Homebrew bottles:
   cmake/ninja from pip, capstone and ICU built from source, and
   `CXXFLAGS="-nostdinc++ -isystem $(xcrun --show-sdk-path)/usr/include/c++/v1"`
   if the Command Line Tools' libc++ headers are incomplete.

3. **Extract messages and enums:**

   ```bash
   python3 pbparse.py blutter_out/asm schema.json schema.proto
   python3 extract_enums.py blutter_out/objs.txt enums.json schema.proto
   ```

   `schema.proto` is a readable dump of all ~670 messages and ~230 enums.
   Messages whose names occur in more than one app file are qualified with
   the file stem (`CleanParam__CleanTask`), and references resolve to the
   same-file definition.

4. **Generate the integration's subset:**

   ```bash
   python3 gen_schema.py schema.json enums.json \
       ../../custom_components/narwal/narwal_client/schema.py
   ```

   `ROOTS` in `gen_schema.py` lists the messages the integration uses (their
   dependencies are included automatically); `STOP` lists large map/vision
   messages kept as raw bytes. Add a root when you start using a new command.

5. Run `pytest`, and check new behaviour against the real robot.

## Other app data

The bundle's `assets/flutter_assets/packages/app_res_config/lib/json/`
holds per-model tables, keyed by internal model code (product key
`EHf6cRNRGT` is **J4**): `robot_exception/J4/en-US.json` (error code →
message, the source of `error_codes.py`) and `consumables_management/J4/`.

Only extract what the integration needs for interoperability; don't commit
app binaries or decompiled output.
