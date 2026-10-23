import hashlib, os
import requests
from dotenv import load_dotenv

load_dotenv()
URL = "http://fms.petroline.in.ua:8055/api/TransactionsGet"
pw = os.environ["PETROLINE_PASS"].strip()
if len(pw) != 64:
    pw = hashlib.sha256(pw.encode("utf-8")).hexdigest()

body = {
    "login": os.environ["PETROLINE_LOGIN"].strip(),
    "pass": pw,
    "datestart": "22.10.2026 00:00:00",
    "datefinish": "22.10.2026 23:59:59",
}

variants = {
    "JSON": dict(json=body),
    "FORM": dict(data=body),
    "JSON-header + form-body": dict(
        data=requests.compat.urlencode(body),
        headers={"Content-Type": "application/json; charset=utf-8"},
    ),
}
for name, kw in variants.items():
    r = requests.post(URL, timeout=60, **kw)
    print(name, r.status_code, r.text[:200].replace("\n", " "))