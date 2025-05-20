# Chọn image Python chính thức
FROM python:3.10

# Set thư mục làm việc trong container
WORKDIR /app

# Copy tất cả các file trong repo vào container
COPY . /app

# Cài đặt dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Đảm bảo Python output không bị buffer
ENV PYTHONUNBUFFERED=1

# Chạy bot.py
CMD ["python", "bot.py"]
