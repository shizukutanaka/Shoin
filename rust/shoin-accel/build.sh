#!/bin/sh
# Build the optional Rust accelerator and place it inside the shoin package
# as shoin/_shoin_accel.so (git-ignored; the package works without it).
# Usage: rust/shoin-accel/build.sh
set -eu
cd "$(dirname "$0")"
cargo build --release
cp target/release/lib_shoin_accel.so ../../shoin/_shoin_accel.so
echo "installed: shoin/_shoin_accel.so"
