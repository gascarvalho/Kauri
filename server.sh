trap 'exit 0' INT

sleep 2

# Initial Parameter Setup
crypto=$1
fanout=$2
pipedepth=$3
pipelatency=$4
latency=$5
bandwidth=$6
blocksize=$7
nservices=$8
myservice=$9

# If not provided, nservices is set to 2: internal and leaf node services
if [ -z "$nservices" ]; then
  nservices=2
fi

# If not provided, it means that it's a Kollaps experiment
if [ -z "$KAURI_UUID" ]; then
  KAURI_UUID=$KOLLAPS_UUID
fi

# Make sure correct branch is selected for crypto
# cd MSc-Kauri && git pull && git submodule update --recursive --remote
# git checkout latest

cd MSc-Kauri

# Do a quick compile of the branch
#git cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED=ON -DHOTSTUFF_PROTO_LOG=ON && make

sleep 30

id=0
i=0

# Go through the list of servers of the given services to identify the number of servers and the id of this server.

for j in $(seq 1 $nservices)
do
  service="server$j-$KAURI_UUID"  
  for ip in $(dig A $service +short | sort -u)
  do
    for myip in $(ifconfig -a | awk '$1 == "inet" {print $2}')
    do
      if [ ${ip} == ${myip} ]
      then
        id=${i}
        echo "This is: ${ip}"
      fi
    done
    ((i++))
  done
done

sleep 20

# Store all services in the list of IPs, from smallest id service (e.g. first internal nodes then the leaf nodes)
touch ips
for j in $(seq 1 $nservices)
do
  service="server$j-$KAURI_UUID"
  dig A $service +short | sort -u | sed -e 's/$/ 1/' >> ips
done

sleep 5

# Generate the HotStuff config file based on the given parameters
python3 scripts/gen_conf.py --ips "ips" --crypto $crypto --fanout $fanout --pipedepth $pipedepth --pipelatency $pipelatency --block-size $blocksize

sleep 20

echo "Starting Application: #${id}" > log${id}

if ! [ -z "$myservice" ]; then
  echo "My Kollaps service is ${myservice}" >> log${id}
fi

# Startup Kauri
#gdb -ex r -ex bt -ex q --args ./examples/hotstuff-app --conf ./hotstuff.gen-sec${id}.conf >> log${id} 2>&1 &

# Startup Kauri (no gdb)
./examples/hotstuff-app --conf ./hotstuff.gen-sec${id}.conf >> log${id} 2>&1 &


# # Add the failure simulation for Replica 0
if [ ${id} == 0 ]; then
  sleep 30  # Adjust this delay as needed
  
  # Kill the `hotstuff-app` process
  app_pid=$(pgrep -f hotstuff-app)  # Find the PID of the process
  if [ ! -z "$app_pid" ]; then
    echo "Killing hotstuff-app process with PID: $app_pid for Replica 0" >> log${id}
    kill -9 $app_pid  # Terminate the process
  else
    echo "No hotstuff-app process found to kill for Replica 0" >> log${id}
  fi
fi


#Configure Network restrictions
#sudo tc qdisc add dev eth0 root netem delay ${latency}ms limit 400000 rate ${bandwidth}mbit &

#sleep 25

# # Start Client on Host Machine
# if [ ${id} == 0 ]; then
#   # gdb -ex r -ex bt -ex q --args ./examples/hotstuff-client --idx ${id} --iter -10 --max-async 900 > clientlog0 2>&1 &
#   ./examples/hotstuff-client --idx ${id} --iter -10 --max-async 900 --client-target global > clientlog0 2>&1 &
# fi

# Start Client on all machines
#gdb -ex r -ex bt -ex q --args ./examples/hotstuff-client --idx ${id} --iter -10 --max-async 50 > clientlog${id} 2>&1 &

sleep 300

killall hotstuff-client &
killall hotstuff-app &

# Wait for the container to be manually killed
sleep 3000
