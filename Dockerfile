FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

RUN apt-get update && apt-get install -y unzip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 🎯 تحميل نماذج EasyOCR مسبقاً لتفادي انقطاع الاتصال أثناء التشغيل
RUN python3 -c "import easyocr; easyocr.Reader(['ar', 'en'], gpu=False)"

COPY . .

RUN unzip -q -o user_data.zip -d user_data

EXPOSE 10000

CMD ["python", "test.py"]
