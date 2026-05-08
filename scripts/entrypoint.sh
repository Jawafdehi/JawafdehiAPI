#!/bin/bash
set -e

# Run database migrations
echo 'Running migrations...'
python manage.py migrate --noinput

# Start Gunicorn
echo 'Starting Gunicorn...'
exec gunicorn config.wsgi:application --bind 0.0.0.0:8080 --workers 2 --threads 4 --timeout 60 --access-logfile - --error-logfile -
