FROM python:3.15-rc-slim-trixie

WORKDIR /app

RUN pip install flask requests --break-system-packages

COPY app/index.html /app/index.html
COPY app/server.py /app/server.py

EXPOSE 80

CMD ["python", "/app/server.py"]
