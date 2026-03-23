FROM python:3.11-alpine

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache/pip \
    && chmod +x /app/update_a_record.py

CMD ["./update_a_record.py"]