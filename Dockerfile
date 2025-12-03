FROM python:3.9-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY bot_webhook.py .
COPY config.json .

# Expose port
EXPOSE 8080

# Run the application
CMD ["python", "-m", "gunicorn", "-b", "0.0.0.0:8080", "-w", "4", "-k", "sync", "--timeout", "120", "bot_webhook:app"]