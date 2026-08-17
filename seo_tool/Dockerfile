FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Le code est importé comme package `seo_tool` -> on le copie sous /app/seo_tool.
COPY . /app/seo_tool
ENV PYTHONPATH=/app
ENV PORT=8080
ENV SEO_OUTPUT_DIR=/tmp
# 1 worker (cache snapshot en mémoire) ; timeout large car le crawl est synchrone.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 600 seo_tool.webapp:app
