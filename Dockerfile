FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Safe cloud default: dry mode unless env overrides.
ENV ARB_DRY_RUN=true
ENV ARB_ENABLE_LIVE=false
ENV LOG_LEVEL=INFO

CMD ["python", "main.py", "monitor"]
