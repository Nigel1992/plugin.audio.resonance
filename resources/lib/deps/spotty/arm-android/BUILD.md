# Android ARM playback helpers

The AArch64 and ARMv7 playback payloads were rebuilt from michaelherger/librespot at
`c8018223c7d071295996e13a21ce6447f4dd4852` using Rust 1.99.1 and Android NDK r27c.
The legacy authentication payloads are unchanged.

`resonance.patch` keeps the desktop client ID and Linux protocol platform
signature together on Android, matching the credentials used for playback.
Changing only the client ID produced HTTP 400 during client-token acquisition;
the original Android identity produced Login5 INVALID_CREDENTIALS. The patched
build successfully decoded complete tracks on a Shield running Android 11,
including with the original legacy-paired credentials.

The patch also enables release error logging. mDNS and the pipe audio backend
are included. Codec output remains 44.1 kHz stereo signed 16-bit PCM on stdout.
This is a Bionic Android executable; it does not require glibc or root access.

Both rebuilt helpers advertise `connect-bridge` in `--check`.
`scripts/patch_connect_helper.py` and `scripts/resonance_connect.rs` add an opt-in
receiver mode selected by `RESONANCE_CONNECT_BRIDGE=1`. That mode writes framed
PCM and JSON player events to stdout and reads JSON control commands from stdin.
Its sink paces audio at 176400 bytes/second with a three-second reserve for
Kodi's initial probing and prebuffering. Normal single-track stdout stays raw
PCM. The service forwards Connect audio through a bounded localhost WAV stream;
the dedicated legacy helper remains responsible for initial pairing.

Rebuild from Linux/WSL with `bash scripts/build_android_spotty.sh` at the repository
root. The script downloads the Rust compiler, NDK and pinned upstream source into
a dedicated cache and writes the result to `dist/spotty-playback-aarch64`. It does
not replace a bundled or installed helper automatically. Requires curl, git,
Python 3 and a host C compiler. Upstream license: `LICENSE.spotty`.

For 32-bit ARM, run `RESONANCE_ANDROID_ARCH=armv7 bash scripts/build_android_spotty.sh`.
This produces `dist/spotty-playback-armv7`; the bundled ARMv7 filename is
`spotty-playback`.
