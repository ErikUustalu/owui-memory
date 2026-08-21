FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY owui-memory.py .
EXPOSE 8080
CMD ["python", "owui-memory.py"]
