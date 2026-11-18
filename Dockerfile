FROM python:3.11-slim

# Install required dependencies (gcc etc. for building pillow, used by reportlab)
# Pillow (ReportLab dep) needs these runtime libs - piwheels build includes many format backends
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    python3-dev \
    libjpeg-dev \
    zlib1g-dev \
    libtiff6 \
    libopenjp2-7 \
    libfreetype6 \
    liblcms2-2 \
    libwebp7 \
    libimagequant0 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY app /app

# Create directories for storing data and logs
RUN mkdir -p /app/data /app/logs

# Bundle the UI's fonts (Roboto Condensed + Roboto Mono) so it works offline; the UI itself has no other dependencies
RUN mkdir -p /app/static/vendor/fonts \
    && for f in roboto-condensed-latin-400-normal roboto-condensed-latin-500-normal roboto-condensed-latin-600-normal \
                roboto-condensed-latin-700-normal; do \
         curl -fsSL -o "/app/static/vendor/fonts/$f.woff2" "https://cdn.jsdelivr.net/npm/@fontsource/roboto-condensed@5.2.8/files/$f.woff2"; \
       done \
    && for f in roboto-mono-latin-400-normal roboto-mono-latin-500-normal; do \
         curl -fsSL -o "/app/static/vendor/fonts/$f.woff2" "https://cdn.jsdelivr.net/npm/@fontsource/roboto-mono@5.2.8/files/$f.woff2"; \
       done

# Install Python dependencies with pinned versions for compatibility
RUN pip install --no-cache-dir \
    flask==3.0.0 \
    werkzeug==3.0.1 \
    requests==2.31.0 \
    websockets \
    reportlab>=4.0.0 \
    --extra-index-url https://www.piwheels.org/simple

EXPOSE 80/tcp
EXPOSE 8765/tcp

# Healthcheck to verify the service is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:80/stats || exit 1

LABEL version="2.0.0"
LABEL permissions='\
{\
  "ExposedPorts": {\
    "80/tcp": {}\
    ,\
    "8765/tcp": {}\
  },\
  "HostConfig": {\
    "CpuPeriod": 100000,\
    "CpuQuota": 100000,\
    "Binds":["/usr/blueos/extensions/odometer/data:/app/data", "/usr/blueos/extensions/odometer/logs:/app/logs"],\
    "ExtraHosts": ["host.docker.internal:host-gateway"],\
    "PortBindings": {\
      "80/tcp": [\
        {\
          "HostPort": ""\
        }\
      ],\
      "8765/tcp": [\
        {\
          "HostPort": ""\
        }\
      ]\
    }\
  }\
}'
LABEL authors='[\
    {\
        "name": "Tony White",\
        "email": "tonywhite@bluerobotics.com"\
    }\
]'
LABEL company='{\
        "about": "",\
        "name": "Blue Robotics",\
        "email": "support@bluerobotics.com"\
    }'
LABEL type="tool"
LABEL tags='[\
        "maintenance",\
        "logging"\
    ]'
LABEL readme='https://raw.githubusercontent.com/vshie/Odometer/{tag}/README.md'
LABEL links='{\
        "website": "https://github.com/vshie/Odometer",\
        "support": "https://github.com/vshie/Odometer/issues"\
    }'
LABEL requirements="core >= 1.1"

WORKDIR /app
ENTRYPOINT ["python", "main.py"]
