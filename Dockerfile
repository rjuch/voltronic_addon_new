FROM debian:stretch

RUN apt update && apt install -y \
        curl \
        git \
        screen \
        build-essential \
        cmake \
        jq \
        mosquitto-clients

ADD sources/ /opt/
ADD config/ /etc/inverter/

RUN chmod a+x /opt/inverter-mqtt/*.sh

RUN cd /opt/inverter-cli && \
    mkdir bin && cmake . && make && mv inverter_poller bin/

HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=1m \
    --retries=3 \
  CMD /opt/healthcheck

WORKDIR /opt
ENTRYPOINT ["/bin/bash", "/opt/inverter-mqtt/entrypoint.sh"]
