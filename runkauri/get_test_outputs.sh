#!/bin/bash

LOG_FOLDER="logs" # Define a folder to store logs
REPLICA_LOG_FOLDER="${LOG_FOLDER}/replicas"
CLIENT_LOG_FOLDER="${LOG_FOLDER}/clients"
mkdir -p $LOG_FOLDER # Create the log folder if it doesn't exist
mkdir -p $REPLICA_LOG_FOLDER # Subfolder for replica logs
mkdir -p $CLIENT_LOG_FOLDER # Subfolder for client logs

TIMESTAMP=$(date +%F_%T)

replica_index=0
# Collect and print results.
for container in $(docker ps -q -f name="server")
do
  replica_index=$((replica_index+1))

  if [ ! $(docker exec -it $container bash -c "cd MSc-Kauri && test -e log*") ]
  then
    REPLICA_LOG_FILE="${REPLICA_LOG_FOLDER}/log_${TIMESTAMP}_${replica_index}.txt"
    docker exec -it $container bash -c "cd MSc-Kauri && cat log*" > $REPLICA_LOG_FILE
  fi

  if [ ! $(docker exec -it $container bash -c "cd MSc-Kauri && test -e clientlog*") ]
  then
    CLIENT_LOG_FILE="${CLIENT_LOG_FOLDER}/clientlog_${TIMESTAMP}_${replica_index}.txt"
    docker exec -it $container bash -c "cd MSc-Kauri && cat clientlog*" > $CLIENT_LOG_FILE
  fi

done

# Rename replica log files to have correct replica name
while IFS= read -r log_file; do
  replica_index=$(grep -o "ReplicaID [0-9]\+" "$log_file" | grep -o '[0-9]\+' | tail -n 1)
  if [ -n "$replica_index" ]; then
    mv "$log_file" "$REPLICA_LOG_FOLDER/log_${TIMESTAMP}_r${replica_index}.txt"
  fi
done < <(find "$REPLICA_LOG_FOLDER" -regextype posix-egrep -regex '.*_[0-9]+\.txt')

# Rename client log files to have correct replica name
while IFS= read -r log_file; do
  replica_index=$(grep -o "I am client with id = [0-9]\+" "$log_file" | grep -o '[0-9]\+' | tail -n 1)
  if [ -n "$replica_index" ]; then
    mv "$log_file" "$CLIENT_LOG_FOLDER/clientlog_${TIMESTAMP}_c${replica_index}.txt"
  fi
done < <(find "$CLIENT_LOG_FOLDER" -regextype posix-egrep -regex '.*_[0-9]+\.txt')