# Use the slim version of Debian
FROM debian:bullseye-slim

# Set environment variables to reduce interaction during builds
ENV DEBIAN_FRONTEND=noninteractive

# Update package lists and install build dependencies
RUN apt-get update && apt-get install -y \
    git gcc g++ make cmake libuv1-dev libssl-dev libsodium-dev \
    autoconf libnet1-dev libtool pastebinit python3 bash gdb dnsutils \
    nano inetutils-ping net-tools sudo iproute2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Add source files to the container
ADD . /MSc-Kauri
ADD ./server.sh /
ADD ./client.sh /

# Set the default command to start the server
ENTRYPOINT ["/bin/bash", "/server.sh"]