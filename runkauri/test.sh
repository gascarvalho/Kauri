#!/bin/bash

trap "docker stack rm kauriservice" EXIT

FILENAME=kauri.yaml
EXPORT_FILENAME=kauri-temp.yaml

ORIGINAL_STRING=thecmd
QTY1_STRING=theqty1
QTY2_STRING=theqty2

FILENAME2="experiments"
FLINES=$(cat $FILENAME2 | grep "^[^#;]")

LOG_FOLDER="logs" # Define a folder to store logs
mkdir -p $LOG_FOLDER # Create the log folder if it doesn't exist

TIMESTAMP=$(date +%F_%T)

# Function to log messages
logger() {
  echo "$(date +"%Y-%m-%d %T") - $1"
}

# Each LINE in the experiment file is one experimental setup
for LINE in $FLINES
do
  echo '---------------------------------------------------------------'
  echo $LINE
  IFS=':' read -ra split <<< "$LINE"

  sed  "s/${ORIGINAL_STRING}/${split[0]}/g" $FILENAME > $EXPORT_FILENAME
  sed  -i "s/${QTY1_STRING}/${split[1]}/g" $EXPORT_FILENAME
  sed  -i "s/${QTY2_STRING}/${split[2]}/g" $EXPORT_FILENAME

  echo '**********************************************'
  echo "*** This setup needs ${split[3]} physical machines! ***"
  echo '**********************************************'

  for i in {1..1}
  do

    # Deploy experiment
    docker stack deploy --with-registry-auth -c kauri-temp.yaml kauriservice &
     
    # Docker startup time 100s + 1*60s of experiment runtime
    sleep 400

    replica_index=0
    # Collect and print results.
    for container in $(docker ps -q -f name="server")
    do
      replica_index=$((replica_index+1))
      if [ ! $(docker exec -it $container bash -c "cd MSc-Kauri && test -e log0") ]
      then
        LOG_FILE="${LOG_FOLDER}/log_${TIMESTAMP}_${replica_index}.txt"

        docker exec -it $container bash -c "cd MSc-Kauri && cat log*" > $LOG_FILE

      fi
    done

    docker stack rm kauriservice
    sleep 30
  done
done

while IFS= read -r log_file; do
  replica_index=$(grep -o "ReplicaID [0-9]\+" "$log_file" | grep -o '[0-9]\+' | tail -n 1)
  if [ -n "$replica_index" ]; then
    mv "$log_file" "$LOG_FOLDER/log_${TIMESTAMP}_r${replica_index}.txt"
  fi
done < <(find "$LOG_FOLDER" -regextype posix-egrep -regex '.*_[0-9]+\.txt')