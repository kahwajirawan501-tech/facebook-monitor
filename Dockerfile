FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# تثبيت أداة unzip
RUN apt-get update && apt-get install -y unzip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# فك ضغط مجلد الجلسة تلقائياً
RUN unzip -q user_data.zip -d user_data

CMD ["python", "test.py"]
