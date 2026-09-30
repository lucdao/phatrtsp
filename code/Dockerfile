FROM ubuntu:22.04

  # Set environment variables for non-interactive installs and time zone
  ENV DEBIAN_FRONTEND=noninteractive
  ENV TZ=Asia/Ho_Chi_Minh

  # Install tzdata and configure time zone
  RUN apt-get update && apt-get install -y tzdata && \
      ln -fs /usr/share/zoneinfo/Asia/Ho_Chi_Minh /etc/localtime && \
      dpkg-reconfigure --frontend noninteractive tzdata && \
      rm -rf /var/lib/apt/lists/*

  # Install required packages, including curl
  RUN apt-get update && \
      apt-get install -y \
        curl \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        python3 \
        python3-pip \
        python3-gi \
        python3-gst-1.0 && \
      apt-get clean && \
      rm -rf /var/lib/apt/lists/*

  # Ensure non-interactive environment (already set, but kept for clarity)
  ENV DEBIAN_FRONTEND=noninteractive

  # Install additional libraries
  RUN apt-get update && apt-get install -y libblas-dev liblapack-dev gstreamer1.0-libav libgstrtspserver-1.0-dev gstreamer1.0-rtsp

  # ffmpeg is used by the standalone video conversion command
  RUN apt-get update && apt-get install -y ffmpeg && \
      apt-get clean && rm -rf /var/lib/apt/lists/*

  RUN pip install pyyaml
  RUN pip install redis

  # Set working directory and copy files
  WORKDIR /server
  COPY server/. /server
