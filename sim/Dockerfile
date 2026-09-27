FROM python:3.12-slim

WORKDIR /app

# The generator is stdlib-only; only seed_blacklist.py needs a driver, because it
# writes to the database directly. Keeping the single dependency means `sim-run` has
# nothing to install and starts immediately.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copied, not bind-mounted, so the image is usable on its own. compose bind-mounts the
# directory over this for development so edits take effect without a rebuild.
COPY . /app

# The edge agents' data directories, so the generator can write into their inboxes.
# compose mounts the real ./edge-agent/data here.
VOLUME ["/edge-agent-data"]

CMD ["python", "generate_detections.py", "--help"]
