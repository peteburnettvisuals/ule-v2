# Use a lightweight Python base image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy your local files into the container
COPY . .

# Install the Python libraries
RUN pip install --no-cache-dir -r requirements.txt

# Cloud Run expects traffic on port 8080
EXPOSE 8080

# The command to start your C2 app
ENTRYPOINT ["streamlit", "run", "streamlit_app.py", "--server.port=8080", "--server.address=0.0.0.0", "--server.enableCORS=false", "--server.enableXsrfProtection=false"]