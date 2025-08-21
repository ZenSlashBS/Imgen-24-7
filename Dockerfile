# Use official Python slim image for a smaller footprint
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create data directory
RUN mkdir -p /app/data

# Copy the bot code and users.txt
COPY bot.py .
COPY users.txt /app/data/users.txt

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV DB_FILE=/app/data/users.db
ENV DATA_DIR=/app/data
ENV USERS_FILE=users.txt

# Expose port for HTTP health check
EXPOSE 8000

# Run the bot
CMD ["python", "bot.py"]
