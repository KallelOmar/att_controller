# One image for the controller and the simulator: they share requirements.txt.
# Built from the repository root (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yml .
COPY scripts scripts
COPY device_simulator device_simulator
