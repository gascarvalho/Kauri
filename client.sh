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
client_service="client-$KAURI_UUID"

# # My client id
# for ip in $(dig A $client_service +short | sort -u)
#   do
#     for myip in $(ifconfig -a | awk '$1 == "inet" {print $2}')
#     do
#       if [ ${ip} == ${myip} ]
#       then
#         id=${i}
#         echo "(Client) This is: ${ip}"
#       fi
#     done
#     ((i++))
#   done
# done

sleep 25

# Store all services in the list of IPs, from smallest id service (e.g. first internal nodes then the leaf nodes)
touch ips
for j in $(seq 1 $nservices); do
  service="server$j-$KAURI_UUID"
  dig A $service +short | sort -u | sed -e 's/$/ 1/' >>ips
done

sleep 5

# Generate the HotStuff config file based on the given parameters
python3 scripts/gen_conf.py --ips "ips" --crypto $crypto --fanout $fanout --pipedepth $pipedepth --pipelatency $pipelatency --block-size $blocksize

# Capture container's IP and append to logs
my_ip=$(hostname -I | awk '{print $1}')
echo "Container IP: ${my_ip}" >>clientlog0

echo "Starting Reputation Server" >>clientlog0

if ! [ -z "$myservice" ]; then
  echo "My Kollaps service is ${myservice}" >>log${id}
fi

#Configure Network restrictions
#sudo tc qdisc add dev eth0 root netem delay ${latency}ms limit 400000 rate ${bandwidth}mbit &

sleep 25

# Start Client on Host Machine
#if [ ${id} == 0 ]; then
#gdb -ex r -ex bt -ex q --args ./examples/hotstuff-client --idx ${id} --iter -10 --max-async 5000 > clientlog0 2>&1 &
#fi

./examples/hotstuff-client --idx ${id} --iter -10 --max-async 5000 >>clientlog0 2>&1 &

# Start Client on all machines
#gdb -ex r -ex bt -ex q --args ./examples/hotstuff-client --idx ${id} --iter -900 --max-async 900 > clientlog${id} 2>&1 &

sleep 180

killall hotstuff-client &
killall hotstuff-app &

# Wait for the container to be manually killed
sleep 3000
