FROM python:3.10-slim

WORKDIR /app

# Install dependencies first (for layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY src/ src/

# Create a data directory for the database volume
RUN mkdir -p /app/data

# Ensure data directory persists across container restarts
VOLUME ["/app/data"]

EXPOSE 8000

# Execute server
CMD ["python", "-m", "src.server"]
