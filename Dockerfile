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

# Bundle frontend dependencies locally for offline use (no internet required at runtime)
RUN mkdir -p /app/static/vendor/mdi/css /app/static/vendor/mdi/fonts \
    && curl -fsSL -o /app/static/vendor/vuetify.min.css \
       "https://cdn.jsdelivr.net/npm/vuetify@2.6.14/dist/vuetify.min.css" \
    && curl -fsSL -o /app/static/vendor/mdi/css/materialdesignicons.min.css \
       "https://cdn.jsdelivr.net/npm/@mdi/font@6.9.96/css/materialdesignicons.min.css" \
    && curl -fsSL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.eot \
       "https://cdn.jsdelivr.net/npm/@mdi/font@6.9.96/fonts/materialdesignicons-webfont.eot" \
    && curl -fsSL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.woff2 \
       "https://cdn.jsdelivr.net/npm/@mdi/font@6.9.96/fonts/materialdesignicons-webfont.woff2" \
    && curl -fsSL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.woff \
       "https://cdn.jsdelivr.net/npm/@mdi/font@6.9.96/fonts/materialdesignicons-webfont.woff" \
    && curl -fsSL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.ttf \
       "https://cdn.jsdelivr.net/npm/@mdi/font@6.9.96/fonts/materialdesignicons-webfont.ttf" \
    && curl -fsSL -o /app/static/vendor/vue.min.js \
       "https://cdn.jsdelivr.net/npm/vue@2.6.14/dist/vue.min.js" \
    && curl -fsSL -o /app/static/vendor/vuetify.min.js \
       "https://cdn.jsdelivr.net/npm/vuetify@2.6.14/dist/vuetify.min.js" \
    && curl -fsSL -o /app/static/vendor/axios.min.js \
       "https://cdn.jsdelivr.net/npm/axios/dist/axios.min.js" \
    && curl -fsSL -o /app/static/vendor/moment.min.js \
       "https://cdn.jsdelivr.net/npm/moment@2.29.4/moment.min.js" \
    && curl -fsSL -o /app/static/vendor/chart.umd.js \
       "https://cdn.jsdelivr.net/npm/chart.js" \
    && curl -fsSL -o /app/static/vendor/chartjs-adapter-moment.js \
       "https://cdn.jsdelivr.net/npm/chartjs-adapter-moment@1.0.1/dist/chartjs-adapter-moment.js"

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

LABEL version="1.0.1"

ARG IMAGE_NAME

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

ARG AUTHOR=Tony
ARG AUTHOR_EMAIL=tony@bluerobotics.com
LABEL authors='[\
    {\
        "name": "TONY",\
        "email": "${AUTHOR_EMAIL}"\
    }\
]'

ARG MAINTAINER
ARG MAINTAINER_EMAIL
LABEL company='{\
        "about": "",\
        "name": "Tony",\
        "email": "tony@bluerobotics.com"\
    }'
LABEL type="utility"
ARG REPO
ARG OWNER
LABEL readme='https://github.com/vshie/Odometer/README.md'
LABEL links='{\
        "source": "https://github.com/vshie/Odometer"\
    }'
LABEL requirements="core >= 1.1"

WORKDIR /app
ENTRYPOINT ["python", "main.py"]
