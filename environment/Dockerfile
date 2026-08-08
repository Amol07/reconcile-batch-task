FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The project only. Never the solution, never the tests.
COPY project/ /app/

CMD ["bash"]
