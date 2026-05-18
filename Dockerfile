FROM python:3.12-slim
WORKDIR /app
RUN pip install owui_client google-genai aiohttp python-dotenv
COPY main.py .
EXPOSE 8080
CMD ["python", "main.py"]