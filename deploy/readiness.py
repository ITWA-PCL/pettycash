"""DB/Redis probes and anonymous login GET; application middleware still runs."""
import os
import urllib.request

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "deploy.production")
import django
django.setup()
from django.db import connection
import redis

with connection.cursor() as cursor:
    cursor.execute("SELECT 1")
    assert cursor.fetchone() == (1,)
assert redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=3, socket_connect_timeout=3).ping()
request = urllib.request.Request("http://127.0.0.1:8000/login/", headers={
    "Host": "pettycash.windowasia.com",
    "X-Forwarded-Proto": "https",
})
with urllib.request.urlopen(request, timeout=3) as response:
    assert response.status == 200
