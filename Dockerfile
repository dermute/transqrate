# transQrate - media transcoder with AV1 QSV + VMAF-targeted quality.
#
# Built on top of linuxserver.io's ffmpeg image: ffmpeg (currently 8.x) compiled
# with av1_qsv (Intel oneVPL), libvmaf, libopus and the Intel media drivers -
# so nothing has to be compiled from source here.
# Pin a tag (e.g. lscr.io/linuxserver/ffmpeg:8.0) for reproducible builds.
FROM lscr.io/linuxserver/ffmpeg:latest

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/transqrate/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /opt/transqrate/requirements.txt

COPY transqrate /opt/transqrate/transqrate
# s6 services: run the app as the "abc" user (remapped to PUID/PGID by the
# base image's init) and chown /config to it on start
COPY root/ /

ENV CONFIG_DIR=/config \
    PYTHONUNBUFFERED=1 \
    PORT=8585 \
    ATTACHED_DEVICES_PERMS=/dev/dri

WORKDIR /opt/transqrate
VOLUME /config
EXPOSE 8585

# the base image's entrypoint is ffmpeg itself - use its s6-overlay init
# instead, which applies PUID/PGID/UMASK and GPU device permissions before
# starting the app (do not combine with docker's --init / init: true)
ENTRYPOINT ["/init"]
