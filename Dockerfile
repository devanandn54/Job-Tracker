FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY job_tracker.py .
COPY companies.csv .

# Create runtime dirs (state is mounted as a volume on Railway for persistence)
RUN mkdir -p state logs

# Railway injects env vars — entrypoint reads them via config
CMD ["python", "job_tracker.py", "--once"]
