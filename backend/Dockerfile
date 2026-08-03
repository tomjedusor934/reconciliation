FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Microsoft ODBC Driver 18 for SQL Server + unixODBC — required by pyodbc for
# direct datamart queries (source_connection engines). We write the apt source
# line ourselves (single [...] options group with signed-by) instead of piping
# Microsoft's prod.list through sed, which produced a malformed two-bracket
# entry. Pinned to the Debian 12 (bookworm) MS repo — its Release is signed by
# the imported key and installs fine even on a newer base image.
#
# NOTE: do NOT `apt-get purge --auto-remove curl gnupg` afterwards. The ODBC
# driver dlopen()s Kerberos/LDAP/OpenSSL libs (libgssapi-krb5-2, libldap2, …)
# that come in as curl/gnupg dependencies; auto-removing them makes the driver
# fail to load with a misleading "Can't open lib … : file not found".
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

COPY . .

RUN chown -R appuser:appgroup /app

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
