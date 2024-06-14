FROM debian:bullseye-slim

# Install build dependencies
RUN apt-get update
RUN DEBIAN_FRONTEND=noninteractive apt-get -y install git gcc g++ make cmake libuv1-dev libssl-dev libsodium-dev autoconf libnet1-dev libtool pastebinit python3 bash gdb dnsutils nano inetutils-ping net-tools sudo iproute2
#RUN git clone https://github.com/aixoss/gmp && cd gmp && ./configure && make install
#RUN git clone https://github.com/AlphaVideo/MSc-Kauri.git && cd MSc-Kauri && git checkout experimental-reconfiguration && git submodule update --init --recursive && git submodule update --recursive --remote

ADD . /MSc-Kauri
ADD ./server.sh /

ENTRYPOINT ["/bin/bash", "/server.sh"]
