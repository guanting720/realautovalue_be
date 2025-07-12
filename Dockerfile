# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container
COPY . .

# Set the command to run the application using Gunicorn with Uvicorn workers.
# This handles long-running requests without timing out.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 main:app
