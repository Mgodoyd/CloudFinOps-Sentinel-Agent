#!/bin/sh
# Cloud Run injects $PORT; default to 8080 for local runs of the same image.
# `exec` replaces the shell so uvicorn becomes PID 1 and gets SIGTERM.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
