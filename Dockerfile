FROM python:3.13-slim

WORKDIR /app

RUN pip install \
    flask \
    requests \
    gevent \
    gevent-websocket \
    websocket-client \
    --break-system-packages

COPY app/index.html /app/index.html
COPY app/server.py /app/server.py

EXPOSE 80

CMD ["python", "/app/server.py"]