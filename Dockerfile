# Sử dụng image Python slim để giảm kích thước
FROM python:3.10-slim

# Cài đặt các gói hệ thống cần thiết cho pandas, openpyxl, và các thư viện khác
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Thiết lập thư mục làm việc
WORKDIR /app

# Sao chép file requirements trước để tận dụng cache layer khi build lại
COPY requirements.txt ./

# Cài đặt dependencies Python
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Sao chép phần còn lại của source code vào container
COPY . .

# Đảm bảo Python output không bị buffer và set encoding mặc định
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8

# Expose port (tùy chọn, giúp chạy local dễ hơn)
EXPOSE 8443

# Lệnh chạy ứng dụng (nên dùng hypercorn để production-ready, nhưng giữ nguyên nếu bạn muốn gọi trực tiếp file)
CMD ["python", "bot.py"]
