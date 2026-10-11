FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY collector/build_db.py collector/embed.py ./collector/
COPY server ./server
COPY data ./data
# rulings.db is derived and too big for git; build it into the image (seconds).
RUN python collector/build_db.py
ENV PORT=8790
EXPOSE 8790
CMD ["python", "server/app.py"]
