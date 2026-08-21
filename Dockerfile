FROM python:3.12-slim

WORKDIR /app

ARG VERSION=dev
ENV APP_VERSION=$VERSION

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

CMD ["python", "monitor.py"]
