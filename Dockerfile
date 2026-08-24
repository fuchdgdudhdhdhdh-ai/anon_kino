FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY main.py content.py ./

# Render передаёт порт через переменную окружения PORT
ENV PORT=10000
EXPOSE 10000

CMD ["python", "main.py"]
