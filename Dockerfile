# Photo Finder Studio. NOT BUILT OR TESTED where it was written (no Docker daemon there): test before relying on it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PF_DATA=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
VOLUME /data
EXPOSE 8000 2121 60000-60049

# Set PF_PUBLIC_URL (for example https://photos.example.com) so share links use your real address.
CMD ["python", "-m", "studio", "run", "--port", "8000"]
