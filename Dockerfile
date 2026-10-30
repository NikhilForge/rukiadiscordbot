FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the code. Secrets come from --env-file at run time, never the image.
COPY *.py ./

CMD ["python", "bot.py"]
