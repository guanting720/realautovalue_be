#!/bin/sh
gunicorn --bind :8080 --workers 1 --timeout 300 main:app