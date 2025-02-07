#!/bin/bash

CONF_PREFIX="hotstuff.gen"

cleanup() {
    echo ""
    logger "Cleaning up generated files and resources..."

    rm -f ips

    rm -f "${CONF_PREFIX}.conf"      # main config file
    rm -f nodes.txt                  # nodes file
    rm -f "${CONF_PREFIX}-sec"*.conf # replica config files

    logger "Cleanup complete. Exiting."
    exit 0
}

trap cleanup INT TERM

crypto=${1:-"bls"}
fanout=${2:-2}
pipedepth=${3:-1}
pipelatency=${4:-10}
latency=${5:-100}
bandwidth=${6:-25}
blocksize=${7:-1000}
idx=${8:-0} # Default client ID

LOG_DIR="runkauri/logs/reputation"
EXECUTABLE="./examples/hotstuff-client"

mkdir -p "$LOG_DIR"

logger() {
    echo -e "\033[1;32m$(date +"%Y-%m-%d %T") - $1\033[0m" | tee -a "$LOG_DIR/server-${idx}.log"
}

error_logger() {
    echo -e "\033[1;31m$(date +"%Y-%m-%d %T") - ERROR: $1\033[0m" | tee -a "$LOG_DIR/server-${idx}.log"
}

logger "Starting reputation server test script..."

if [ ! -f "$EXECUTABLE" ]; then
    error_logger "Cannot find the client executable at $EXECUTABLE."
    exit 1
fi

logger "Generating local IPs list..."
cat <<EOF >ips
127.0.0.1 1
127.0.0.2 1
127.0.0.3 1
127.0.0.4 1
127.0.0.5 1
127.0.0.6 1
127.0.0.7 1
EOF

logger "Generating configuration file using the Python script..."
python3 scripts/gen_conf.py --prefix "$CONF_PREFIX" --ips "ips" --crypto "$crypto" --fanout "$fanout" --pipedepth "$pipedepth" --pipelatency "$pipelatency" --block-size "$blocksize"

if [ $? -ne 0 ]; then
    error_logger "Failed to generate configuration file."
    cleanup
fi

logger "Running make command..."
make

if [ $? -ne 0 ]; then
    error_logger "Build failed. Check make logs for details."
    cleanup
fi

logger "Starting HotStuff client locally with ID $idx..."
./examples/hotstuff-client --idx "$idx" --iter -10 --max-async 5000

cleanup
