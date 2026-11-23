FROM python:3.12-alpine
WORKDIR /app
COPY neutralis-btc-alerta/app/ /app/
EXPOSE 8080
ENV PYTHONUNBUFFERED=1 APP_DATA_DIR=/data
CMD ["python", "server.py"]
