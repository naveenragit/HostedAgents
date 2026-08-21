# Container Apps job image for the daily billed-cost sync.
#
# linux/amd64 is the only architecture the platform accepts, so the build runs in
# ACR (`az acr build`) rather than on a developer machine, which is ARM64 here.
FROM mcr.microsoft.com/azurelinux/base/python:3.12

WORKDIR /app

# The Azure Linux base image ships `python3` only, and its pip comes from an rpm
# with no RECORD file, so `pip install --upgrade pip` cannot uninstall it and
# fails the build. The bundled pip is used as-is.
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY billing_sync/ ./billing_sync/

# The job runs to completion and exits; a non-zero code marks the execution
# failed so it surfaces in the job's execution history.
CMD ["python3", "main.py"]
