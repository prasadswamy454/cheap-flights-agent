FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cheap_flights_agent ./cheap_flights_agent

EXPOSE 8000

CMD ["python", "-m", "cheap_flights_agent.web"]
