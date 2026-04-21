FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Safe cloud defaults for both strategies.  Live mode requires explicit
# overrides of the *_DRY_RUN and *_ENABLE_LIVE vars per strategy.
ENV ARB_DRY_RUN=true
ENV ARB_ENABLE_LIVE=false
ENV SNIPE_DRY_RUN=true
ENV SNIPE_ENABLE_LIVE=false
ENV LOG_LEVEL=INFO

# Default entrypoint is the snipe strategy.  To run the legacy arb bot
# instead, override the start command in Railway to: python main.py monitor
CMD ["python", "-m", "snipe.main", "run", "--yes"]
