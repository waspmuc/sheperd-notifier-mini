FROM python:3.13-alpine
WORKDIR /app
COPY server.py .
EXPOSE 8000
CMD ["python", "-u", "server.py"]
