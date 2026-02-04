# 1. Use a lightweight Python image
FROM python:3.11-slim

# 2. Prevent Python from buffering logs (makes debugging easier in Cloud Run)
ENV PYTHONUNBUFFERED=1

# 3. Set the working directory
WORKDIR /app

# 4. Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the rest of your code
COPY . .

# 6. Streamlit's default port is 8501, but Cloud Run expects 8080
EXPOSE 8080

# 7. Start Streamlit, mapping the port to 8080
CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8080", "--server.address=0.0.0.0"]