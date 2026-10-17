# ハブ(desk_monitor.hub)の実行環境。ソースとモデルは compose.yaml でマウントする。
FROM python:3.13-slim

# 日本語ラベルの描画用フォントは assets/fonts を使うので、ここでは入れない
WORKDIR /app
COPY requirements.txt requirements-hub.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements-hub.txt

ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
CMD ["python", "-m", "desk_monitor.hub"]
