# ------------------------------
# Python FastAPI image for Railway
# ------------------------------

# Use a lightweight, current‑stable Python image
FROM python:3.12-slim

# Prevent Python from buffering stdout/stderr (helps Railway logs)
ENV PYTHONUNBUFFERED=1

# Set a working directory inside the container
WORKDIR /app

# ---------- Install dependencies ----------
# Copy only the lockfile/requirements first so Docker can cache this layer
COPY requirements.txt .

# Install everything from requirements.txt (fastapi, uvicorn, psycopg2‑binary, etc.)
RUN pip install --no-cache-dir -r requirements.txt
# Ensure FastAPI and Uvicorn are installed (fallback if requirements missing)
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# ---------- Copy source ----------
# Now copy the rest of the application code
COPY . .

# ---------- Runtime command ----------
# Railway will inject the $PORT environment variable at runtime.
# Using shell variable expansion lets uvicorn see the actual port number.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]