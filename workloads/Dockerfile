FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /work
RUN pip install --no-cache-dir "Pillow>=12.1.1,<13"
COPY batch.py images.py /work/
USER 65534:65534
ENTRYPOINT ["python", "-u", "/work/batch.py"]
CMD ["--seconds", "5"]
