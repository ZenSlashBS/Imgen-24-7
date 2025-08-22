FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data
COPY bot.py .
COPY users.txt /app/data/users.txt

ENV PYTHONUNBUFFERED=1
ENV DB_FILE=/app/data/users.db
ENV DATA_DIR=/app/data
ENV USERS_FILE=users.txt
ENV LOCK_FILE=/app/data/bot.lock
ENV PORT=8080

EXPOSE 8080

CMD ["python", "bot.py"]
