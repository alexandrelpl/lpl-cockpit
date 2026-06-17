FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
# 1 worker (et non 2) : le cache mémoire des endpoints (_BQ_CACHE) est par-process.
# Un seul worker = un seul cache partagé par toutes les requêtes -> bien meilleur taux de hit
# pour un outil interne à faible trafic. Le parallélisme reste assuré par les threads.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 120 app:app
