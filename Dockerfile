# Transqode - media transcoder with AV1 QSV + VMAF-targeted quality.
#
# Built on top of linuxserver.io's ffmpeg image: ffmpeg (currently 8.x) compiled
# with av1_qsv (Intel oneVPL), libvmaf, libopus and the Intel media drivers -
# so nothing has to be compiled from source here.
# Pin a tag (e.g. lscr.io/linuxserver/ffmpeg:8.0) for reproducible builds.
FROM lscr.io/linuxserver/ffmpeg:latest

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/transqode/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /opt/transqode/requirements.txt

COPY transqode /opt/transqode/transqode

ENV CONFIG_DIR=/config \
    PYTHONUNBUFFERED=1 \
    PORT=8585

WORKDIR /opt/transqode
VOLUME /config
EXPOSE 8585

# the base image's entrypoint is ffmpeg itself - replace it with the app
ENTRYPOINT []
CMD ["python3", "-m", "uvicorn", "transqode.main:app", "--host", "0.0.0.0", "--port", "8585"]
