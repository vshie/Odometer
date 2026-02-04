FROM python:3.11-slim

# Install required dependencies
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY app /app

# Create directories for storing data and logs
RUN mkdir -p /app/data /app/logs

# Install Python dependencies with pinned versions for compatibility
RUN pip install --no-cache-dir \
    flask==3.0.0 \
    werkzeug==3.0.1 \
    requests==2.31.0 \
    --extra-index-url https://www.piwheels.org/simple

EXPOSE 7042/tcp

# Healthcheck to verify the service is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:7042/stats || exit 1

LABEL version="1.0.1"

ARG IMAGE_NAME

LABEL permissions='\
{\
  "ExposedPorts": {\
    "7042/tcp": {}\
  },\
  "HostConfig": {\
    "CpuPeriod": 100000,\
    "CpuQuota": 100000,\
    "Binds":["/usr/blueos/extensions/odometer/data:/app/data", "/usr/blueos/extensions/odometer/logs:/app/logs"],\
    "ExtraHosts": ["host.docker.internal:host-gateway"],\
    "PortBindings": {\
      "7042/tcp": [\
        {\
          "HostPort": ""\
        }\
      ]\
    }\
  }\
}'

ARG AUTHOR
ARG AUTHOR_EMAIL
LABEL authors='[\
    {\
        "name": "TONY",\
        "email": "$tony@bluerobotics.com"\
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
