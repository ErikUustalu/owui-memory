FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
COPY owui-memory.py .
COPY default_system_prompts.py .
RUN pip install -r requirements.txt
EXPOSE 8080
CMD ["python", "owui-memory.py"]
