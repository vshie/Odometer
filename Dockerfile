FROM python:3.11-slim

# Install required dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY app /app

# Create directories for storing data and logs
RUN mkdir -p /app/data /app/logs

# Install Python dependencies
RUN pip install --no-cache-dir \
    litestar[standard]==2.12.1 \
    requests==2.31.0 \
    --extra-index-url https://www.piwheels.org/simple

EXPOSE 7042/tcp

LABEL version="0.1.0"

ARG IMAGE_NAME

LABEL permissions='\
{\
  "ExposedPorts": {\
    "7042/tcp": {}\
  },\
  "HostConfig": {\
    "Binds":["/usr/blueos/extensions/$IMAGE_NAME/data:/app/data", "/usr/blueos/extensions/$IMAGE_NAME/logs:/app/logs"],\
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
LABEL readme='https://raw.githubusercontent.com/$OWNER/$REPO/{tag}/README.md'
LABEL links='{\
        "source": "https://github.com/$OWNER/$REPO"\
    }'
LABEL requirements="core >= 1.1"

WORKDIR /app
ENTRYPOINT ["litestar", "run", "--host", "0.0.0.0", "--port", "7042"]
