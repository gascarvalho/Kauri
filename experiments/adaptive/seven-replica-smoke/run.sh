#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository=$(CDPATH= cd -- "$script_dir/../../.." && pwd)

cmake_bin=${KAURI_CMAKE:-cmake}
build_dir=${KAURI_BUILD_DIR:-"$repository/build-adaptive"}
build_type=${KAURI_BUILD_TYPE:-RelWithDebInfo}

case "$build_dir" in
    /*) ;;
    *) build_dir="$repository/$build_dir" ;;
esac

"$cmake_bin" -S "$repository" -B "$build_dir" \
    -DBUILD_EXAMPLES=ON \
    -DBUILD_SHARED=OFF \
    -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_BUILD_TYPE="$build_type" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5

if [ -n "${KAURI_BUILD_JOBS:-}" ]; then
    "$cmake_bin" --build "$build_dir" \
        --target hotstuff-app hotstuff-client \
        hotstuff-keygen hotstuff-tls-keygen \
        --parallel "$KAURI_BUILD_JOBS"
else
    "$cmake_bin" --build "$build_dir" \
        --target hotstuff-app hotstuff-client \
        hotstuff-keygen hotstuff-tls-keygen \
        --parallel
fi

exec python3 "$script_dir/run_smoke.py" \
    --repository "$repository" \
    --app-binary "$build_dir/examples/hotstuff-app" \
    --manager-binary "$build_dir/examples/hotstuff-client" \
    --keygen-binary "$build_dir/hotstuff-keygen" \
    --tls-keygen-binary "$build_dir/hotstuff-tls-keygen" \
    "$@"
