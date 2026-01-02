# trunk-ignore-all(trivy)
# trunk-ignore-all(checkov)

FROM ubuntu:22.04

# Prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    make \
    gcc \
    g++ \
    libmariadb3 \
    libmariadb-dev \
    libpq-dev \
    libffi-dev \
    musl-dev \
    curl \
    ca-certificates \
    libmagic-dev \
    7zip \
    tzdata \
    libbz2-dev \
    libssl-dev \
    libreadline-dev \
    libsqlite3-dev \
    zlib1g-dev \
    liblzma-dev \
    libncurses5-dev \
    libncursesw5-dev \
    xvfb \
    retroarch \
    netcat-openbsd \
    xdotool \
    imagemagick \
    x11-xserver-utils \
    libgl1-mesa-dri \
    libgl1-mesa-glx \
    libsdl2-2.0-0 \
    pulseaudio \
    pulseaudio-utils \
    dbus \
    unzip \
    # GStreamer for WebRTC streaming
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-pulseaudio \
    gstreamer1.0-x \
    gstreamer1.0-nice \
    # TURN server for WebRTC NAT traversal
    coturn \
    # PyGObject build dependencies
    libcairo2-dev \
    libgirepository1.0-dev \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install nvm
ENV NVM_DIR="/root/.nvm"
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash \
    && . "$NVM_DIR/nvm.sh" \
    && nvm install 18.20.8 \
    && nvm use 18.20.8 \
    && nvm alias default 18.20.8
ENV PATH="$NVM_DIR/versions/node/v18.20.8/bin:$PATH"

# Build and install RAHasher (optional for RA hashes)
RUN git clone --recursive --branch 1.8.1 --depth 1 https://github.com/RetroAchievements/RALibretro.git /tmp/RALibretro
WORKDIR /tmp/RALibretro
RUN sed -i '22a #include <ctime>' ./src/Util.h \
    && sed -i '6a #include <unistd.h>' \
      ./src/libchdr/deps/zlib-1.3.1/gzlib.c \
      ./src/libchdr/deps/zlib-1.3.1/gzread.c \
      ./src/libchdr/deps/zlib-1.3.1/gzwrite.c \
    && make HAVE_CHD=1 -f ./Makefile.RAHasher \
    && cp ./bin64/RAHasher /usr/bin/RAHasher
RUN rm -rf /tmp/RALibretro

# Download RetroArch cores for development
# Cores are downloaded from libretro buildbot nightly builds
RUN mkdir -p /usr/lib/libretro && \
    cd /tmp && \
    for core in \
        # Nintendo consoles
        fceumm nestopia mesen \
        snes9x bsnes \
        mupen64plus_next parallel_n64 \
        dolphin \
        # Nintendo handhelds
        gambatte sameboy mgba vba_next \
        desmume melonds \
        citra pokemini \
        mednafen_vb \
        # Sega consoles
        genesis_plus_gx picodrive \
        mednafen_saturn yabause kronos \
        flycast \
        # Sony consoles
        pcsx_rearmed mednafen_psx_hw swanstation \
        pcsx2 ppsspp \
        # Atari
        stella a5200 prosystem atari800 hatari \
        handy mednafen_lynx virtualjaguar \
        # NEC
        mednafen_pce mednafen_pce_fast mednafen_supergrafx mednafen_pcfx \
        quasi88 np2kai \
        # Arcade
        fbneo mame mame2003_plus neocd mednafen_ngp \
        # Other consoles
        opera gearcoleco freeintv o2em vecx freechaf same_cdi bluemsx \
        # Computers
        vice_x64 vice_xvic vice_x128 vice_xplus4 vice_xpet \
        puae cap32 crocods fuse 81 fmsx \
        dosbox_pure dosbox_svn px68k x1 scummvm \
        # Handhelds
        mednafen_wswan gw sameduck potator vemulator uzem wasm4 theodore \
    ; do \
        echo "Downloading ${core}_libretro.so..."; \
        curl -L -o "${core}_libretro.so.zip" "http://buildbot.libretro.com/nightly/linux/x86_64/latest/${core}_libretro.so.zip" 2>/dev/null && \
        unzip -o "${core}_libretro.so.zip" -d /usr/lib/libretro/ 2>/dev/null && \
        rm -f "${core}_libretro.so.zip" || echo "Failed to download ${core}, skipping..."; \
    done && \
    ls -lh /usr/lib/libretro/

# Install frontend dependencies
COPY frontend/package.json /app/frontend/
WORKDIR /app/frontend
RUN npm install

# Set working directory
WORKDIR /app

# Install uv for the non-root user
COPY --from=ghcr.io/astral-sh/uv:0.7.19 /uv /uvx /usr/local/bin/

# Install Python
RUN uv python install 3.13

# Copy project files (including pyproject.toml and uv.lock)
COPY pyproject.toml uv.lock* .python-version /app/

# Install Python dependencies
RUN uv sync --all-extras

ENV PATH="/app/.venv/bin:${PATH}"

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
