#!/bin/bash

trap "docker stack rm kauriservice" EXIT

FILENAME=kauri.yaml
EXPORT_FILENAME=kauri-temp.yaml

COMMAND_STRING=thecmd
QTY1_STRING=theqty1
QTY2_STRING=theqty2

FILENAME2="experiments-v2"
FLINES=$(cat $FILENAME2 | grep "^[^#;]")

LOG_FOLDER="logs" # Define a folder to store logs
REPLICA_LOG_FOLDER="${LOG_FOLDER}/replicas"
CLIENT_LOG_FOLDER="${LOG_FOLDER}/clients"

rm -f $REPLICA_LOG_FOLDER/*
rm -f $CLIENT_LOG_FOLDER/*

mkdir -p $LOG_FOLDER         # Create the log folder if it doesn't exist
mkdir -p $REPLICA_LOG_FOLDER # Subfolder for replica logs
mkdir -p $CLIENT_LOG_FOLDER  # Subfolder for client logs

# Function to log messages
logger() {
  echo "$(date +"%Y-%m-%d %T") - $1"
}

# Each LINE in the experiment file is one experimental setup
for LINE in $FLINES; do
  echo '---------------------------------------------------------------'
  echo $LINE
  IFS=':' read -ra split <<<"$LINE"

  sed "s/${COMMAND_STRING}/${split[0]}/g" $FILENAME >$EXPORT_FILENAME
  sed -i "s/${QTY1_STRING}/${split[1]}/g" $EXPORT_FILENAME
  sed -i "s/${QTY2_STRING}/${split[2]}/g" $EXPORT_FILENAME

  echo '**********************************************'
  echo "*** This setup needs ${split[3]} physical machines! ***"
  echo '**********************************************'

  #the command == data between [] in the test inputs

  for i in {1..1}; do
    TIMESTAMP=$(date +%F_%T)

    # Deploy experiment
    docker stack deploy -c kauri-temp.yaml kauriservice &

    # Docker startup time 100s + 1*60s of experiment runtime
    sleep 400

    replica_index=0
    # Collect and print results.
    for container in $(docker ps -q -f name="server"); do
      replica_index=$((replica_index + 1))

      if [ ! $(docker exec -it $container bash -c "cd MSc-Kauri && test -e log*") ]; then
        REPLICA_LOG_FILE="${REPLICA_LOG_FOLDER}/log_${TIMESTAMP}_${replica_index}.txt"
        docker exec -it $container bash -c "cd MSc-Kauri && cat log*" >$REPLICA_LOG_FILE
      fi

      if [ ! $(docker exec -it $container bash -c "cd MSc-Kauri && test -e clientlog*") ]; then
        CLIENT_LOG_FILE="${CLIENT_LOG_FOLDER}/clientlog_${TIMESTAMP}_${replica_index}.txt"
        docker exec -it $container bash -c "cd MSc-Kauri && cat clientlog*" >$CLIENT_LOG_FILE
      fi

    done

    for container in $(docker ps -q -f name="client"); do
      #add iterator

      if [ ! $(docker exec -it $container bash -c "cd MSc-Kauri && test -e log*") ]; then
        REPLICA_LOG_FILE="${REPLICA_LOG_FOLDER}/log_${TIMESTAMP}_999.txt"
        docker exec -it $container bash -c "cd MSc-Kauri && cat log*" >$REPLICA_LOG_FILE
      fi

      if [ ! $(docker exec -it $container bash -c "cd MSc-Kauri && test -e clientlog*") ]; then
        CLIENT_LOG_FILE="${CLIENT_LOG_FOLDER}/clientlog_${TIMESTAMP}_999.txt"
        docker exec -it $container bash -c "cd MSc-Kauri && cat clientlog*" >$CLIENT_LOG_FILE
      fi

    done

    docker stack rm kauriservice

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

    sleep 30
  done
done
