#!/usr/bin/env bash
set -euo pipefail
task_repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_build="${RESONANCE_BUILD_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/resonance-android-build}"
task_revision=c8018223c7d071295996e13a21ce6447f4dd4852
task_patch="$task_repo/resources/lib/deps/spotty/arm-android/resonance.patch"
task_arch="${RESONANCE_ANDROID_ARCH:-aarch64}"
case "$task_arch" in
  aarch64) task_target=aarch64-linux-android; task_clang=aarch64-linux-android26-clang ;;
  armv7) task_target=armv7-linux-androideabi; task_clang=armv7a-linux-androideabi26-clang ;;
  *) echo "Unsupported Android architecture: $task_arch" >&2; exit 1 ;;
esac
mkdir -p "$task_build"
cd "$task_build"
export CARGO_HOME="$task_build/cargo"
export RUSTUP_HOME="$task_build/rustup"
export PATH="$CARGO_HOME/bin:$PATH"
if ! command -v cargo >/dev/null; then
  curl --fail --location https://sh.rustup.rs -o rustup-init.sh
  sh rustup-init.sh -y --profile minimal --default-toolchain 1.99.1 --no-modify-path
fi
rustup toolchain install 1.99.1 --profile minimal
rustup target add "$task_target" --toolchain 1.99.1
if [ ! -d android-ndk-r27c ]; then
  curl --fail --location --retry 2 https://dl.google.com/android/repository/android-ndk-r27c-linux.zip -o ndk.zip
  python3 - <<'PY'
import os
import zipfile
with zipfile.ZipFile('ndk.zip') as archive:
    for item in archive.infolist():
        if item.filename.startswith('android-ndk-r27c/toolchains/llvm/prebuilt/linux-x86_64/'):
            path = archive.extract(item)
            mode = item.external_attr >> 16
            if mode:
                os.chmod(path, mode)
PY
fi
python3 - <<'PY'
import os
import pathlib
import stat
import zipfile
with zipfile.ZipFile('ndk.zip') as archive:
    for item in archive.infolist():
        if item.filename.startswith('android-ndk-r27c/toolchains/llvm/prebuilt/linux-x86_64/') and stat.S_ISLNK(item.external_attr >> 16):
            path = pathlib.Path(item.filename)
            if not path.is_symlink():
                path.unlink(missing_ok=True)
                path.symlink_to(archive.read(item).decode())
PY
if [ ! -d source/.git ]; then
  git clone --depth 1 --branch spotty https://github.com/michaelherger/librespot.git source
fi
cd source
if [ "$(git rev-parse HEAD)" != "$task_revision" ]; then
  git fetch --depth 1 origin "$task_revision"
  git checkout --detach "$task_revision"
fi
git rev-parse HEAD > ../source-revision.txt
if git apply --check "$task_patch" 2>/dev/null; then
  git apply "$task_patch"
elif ! git apply --reverse --check "$task_patch"; then
  echo 'Source has unexpected changes; refusing to overwrite them.' >&2
  exit 1
fi
python3 - <<'PY'
from pathlib import Path
Path('src/client_id.txt').write_text('2eb96f9b37494be1824999d58028a305')
PY
python3 "$task_repo/scripts/patch_connect_helper.py" "$task_repo"
ndk_bin="$task_build/android-ndk-r27c/toolchains/llvm/prebuilt/linux-x86_64/bin"
export CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$ndk_bin/aarch64-linux-android26-clang"
export CC_aarch64_linux_android="$ndk_bin/aarch64-linux-android26-clang"
export AR_aarch64_linux_android="$ndk_bin/llvm-ar"
export CARGO_TARGET_ARMV7_LINUX_ANDROIDEABI_LINKER="$ndk_bin/$task_clang"
export CC_armv7_linux_androideabi="$ndk_bin/$task_clang"
export AR_armv7_linux_androideabi="$ndk_bin/llvm-ar"
export CARGO_PROFILE_RELEASE_LTO=false
export CARGO_BUILD_JOBS=4
cargo +1.99.1 build --locked --release --target "$task_target" --no-default-features --features spotty,rustls-tls-webpki-roots,with-libmdns
mkdir -p "$task_repo/dist"
cp "target/$task_target/release/spotty" "$task_repo/dist/spotty-playback-$task_arch"
echo "Android playback helper ready in dist/spotty-playback-$task_arch."
