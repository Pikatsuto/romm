#!/bin/bash

set -e

echo "Starting entrypoint script..."

# Create symlinks for frontend
for subfolder in assets resources; do
	if [[ -L /app/frontend/assets/romm/${subfolder} ]]; then
		target=$(readlink "/app/frontend/assets/romm/${subfolder}")

		# If the target is not the same as ${ROMM_BASE_PATH}/${subfolder}, recreate the symbolic link.
		if [[ ${target} != "${ROMM_BASE_PATH}/${subfolder}" ]]; then
			rm "/app/frontend/assets/romm/${subfolder}"
			ln -s "${ROMM_BASE_PATH}/${subfolder}" "/app/frontend/assets/romm/${subfolder}"
		fi
	elif [[ ! -e /app/frontend/assets/romm/${subfolder} ]]; then
		# Ensure parent directory exists before creating symbolic link
		mkdir -p "/app/frontend/assets/romm"
		ln -s "${ROMM_BASE_PATH}/${subfolder}" "/app/frontend/assets/romm/${subfolder}"
	fi
done

# Define a signal handler to propagate termination signals
function handle_termination() {
	echo "Terminating child processes..."
	# Kill all background jobs
	# trunk-ignore(shellcheck)
	kill -TERM $(jobs -p) 2>/dev/null
}

# Trap SIGTERM and SIGINT signals
trap handle_termination SIGTERM SIGINT

# Set ROMM_AUTH_SECRET_KEY if not already set
if [[ -z ${ROMM_AUTH_SECRET_KEY} ]]; then
	ROMM_AUTH_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
	export ROMM_AUTH_SECRET_KEY
fi

# Start RetroArch support services FIRST (before backend) so env vars are available
if [[ ${RETROARCH_ENABLED} == "true" ]]; then
	# Start D-Bus (required by PulseAudio)
	mkdir -p /run/dbus
	rm -f /run/dbus/pid  # Clean up stale pid file from previous run
	dbus-daemon --system --fork

	# Configure PulseAudio for system mode with anonymous access
	mkdir -p /etc/pulse
	cat > /etc/pulse/system.pa << 'PULSE_CONFIG'
# PulseAudio system mode configuration for RetroArch streaming
load-module module-native-protocol-unix auth-anonymous=1
load-module module-null-sink sink_name=default_sink sink_properties=device.description="Default"
set-default-sink default_sink
PULSE_CONFIG

	# Start PulseAudio daemon (--system required when running as root)
	echo "Starting PulseAudio in system mode..."
	# Clean up stale runtime files from previous run
	rm -rf /run/pulse /var/run/pulse
	mkdir -p /run/pulse
	chown pulse:pulse /run/pulse
	pulseaudio --system --daemonize --exit-idle-time=-1
	sleep 1

	# Start coturn TURN server for WebRTC NAT traversal
	echo "Starting coturn TURN server..."

	# Generate random credentials for TURN server
	TURN_USER="romm"
	TURN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(16))")
	TURN_PORT=${RETROARCH_TURN_PORT:-3478}
	TURN_REALM="romm.local"

	# Auto-detect external IP for TURN server if not set
	# This needs to happen BEFORE creating coturn config
	if [[ -z ${RETROARCH_TURN_EXTERNAL_HOST} ]]; then
		# Try host.docker.internal first (requires extra_hosts in docker-compose)
		if getent hosts host.docker.internal > /dev/null 2>&1; then
			HOST_IP=$(getent hosts host.docker.internal | awk '{print $1}')
			RETROARCH_TURN_EXTERNAL_HOST="${HOST_IP}"
			echo "Using host.docker.internal IP for TURN: ${HOST_IP}"
		else
			# Fallback to Docker gateway
			HOST_IP=$(ip route | grep default | awk '{print $3}')
			if [[ -n ${HOST_IP} ]]; then
				RETROARCH_TURN_EXTERNAL_HOST="${HOST_IP}"
				echo "Using Docker gateway IP for TURN: ${HOST_IP}"
			else
				# Last resort fallback
				RETROARCH_TURN_EXTERNAL_HOST="127.0.0.1"
				echo "WARNING: Could not detect host IP, using localhost for TURN (may fail)"
			fi
		fi
		echo "NOTE: For external access, set RETROARCH_TURN_EXTERNAL_HOST to your host's LAN IP"
	fi

	# Create coturn configuration
	# Get the external IP for coturn relay (must be reachable from browser)
	COTURN_EXTERNAL_IP="${RETROARCH_TURN_EXTERNAL_HOST}"

	# Get the container's internal IP for the external-ip mapping
	CONTAINER_IP=$(hostname -I | awk '{print $1}')
	echo "Container IP: ${CONTAINER_IP}, External IP: ${COTURN_EXTERNAL_IP}"

	cat > /tmp/turnserver.conf << TURNCONF
# Coturn low-latency configuration for cloud gaming
listening-port=${TURN_PORT}
fingerprint
lt-cred-mech
realm=${TURN_REALM}
user=${TURN_USER}:${TURN_PASSWORD}

# External IP mapping
external-ip=${COTURN_EXTERNAL_IP}/${CONTAINER_IP}
external-ip=${COTURN_EXTERNAL_IP}
relay-ip=${CONTAINER_IP}
listening-ip=0.0.0.0

# Port range for relay
min-port=10100
max-port=10120

# Performance optimizations
no-software-attribute
no-stun-backward-compatibility
stale-nonce=0
max-bps=0

# Disable unused features
no-loopback-peers
no-multicast-peers
no-tls
no-dtls
no-cli

# Minimal logging (errors only)
log-file=stdout
no-stdout-log
TURNCONF

	# Start coturn in background
	turnserver -c /tmp/turnserver.conf &
	sleep 1

	# Set TURN server URL for the daemon (internal Docker format)
	# The daemon will use this to configure GStreamer webrtcbin
	export RETROARCH_TURN_SERVER="turn://${TURN_USER}:${TURN_PASSWORD}@127.0.0.1:${TURN_PORT}"

	# Export TURN config for backend to read
	export RETROARCH_TURN_EXTERNAL_HOST
	export RETROARCH_TURN_USER="${TURN_USER}"
	export RETROARCH_TURN_PASSWORD="${TURN_PASSWORD}"
	export RETROARCH_TURN_PORT="${TURN_PORT}"

	echo "TURN server started on port ${TURN_PORT} (external host: ${RETROARCH_TURN_EXTERNAL_HOST})"
fi

# Start all services in the background
echo "Starting backend..."
cd /app/backend
uv run python main.py &

echo "Starting RQ scheduler..."
RQ_REDIS_HOST=${REDIS_HOST:-127.0.0.1} \
	RQ_REDIS_PORT=${REDIS_PORT:-6379} \
	RQ_REDIS_USERNAME=${REDIS_USERNAME:-""} \
	RQ_REDIS_PASSWORD=${REDIS_PASSWORD:-""} \
	RQ_REDIS_DB=${REDIS_DB:-0} \
	RQ_REDIS_SSL=${REDIS_SSL:-0} \
	rqscheduler \
	--path /app/backend \
	--pid /tmp/rq_scheduler.pid &

echo "Starting RQ worker..."
# Build Redis URL properly
if [[ -n ${REDIS_PASSWORD-} ]]; then
	REDIS_URL="redis${REDIS_SSL:+s}://${REDIS_USERNAME-}:${REDIS_PASSWORD}@${REDIS_HOST:-127.0.0.1}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
elif [[ -n ${REDIS_USERNAME-} ]]; then
	REDIS_URL="redis${REDIS_SSL:+s}://${REDIS_USERNAME}@${REDIS_HOST:-127.0.0.1}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
else
	REDIS_URL="redis${REDIS_SSL:+s}://${REDIS_HOST:-127.0.0.1}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
fi

# Set PYTHONPATH so RQ can find the tasks module
PYTHONPATH="/app/backend:${PYTHONPATH-}" rq worker \
	--path /app/backend \
	--pid /tmp/rq_worker.pid \
	--url "${REDIS_URL}" \
	high default low &

echo "Starting watcher..."
watchfiles \
	--target-type command \
	'uv run python watcher.py' \
	/app/romm/library &

# Start RetroArch streaming daemon if enabled (services already started above)
if [[ ${RETROARCH_ENABLED} == "true" ]]; then
	echo "Starting RetroArch streaming daemon..."
	PYTHONPATH="/app/backend:${PYTHONPATH-}" python3 /app/backend/services/retroarch_daemon.py &
fi

# Start the frontend dev server
cd /app/frontend
npm run dev &

# Wait for all background processes
wait
