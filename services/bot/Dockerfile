FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -U pip && pip install --no-cache-dir -r requirements.txt
COPY app /app/app
CMD ["python", "-m", "app.main"]
