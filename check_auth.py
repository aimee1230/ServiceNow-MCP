"""Diagnose ServiceNow authentication (OAuth client-credentials or basic).
Run from the folder that contains your .env:   python check_auth.py
Needs: pip install httpx python-dotenv
"""
import os
import sys
import httpx
from dotenv import dotenv_values

vals = dotenv_values(".env")
g = lambda k: (vals.get(k) or "").strip()
instance, mode = g("SERVICENOW_INSTANCE").rstrip("/"), (g("SERVICENOW_AUTH_MODE") or "oauth").lower()

print("== 1. Config read from .env ==")
print("instance :", instance)
print("mode     :", mode)
for k in ("SERVICENOW_USERNAME", "SERVICENOW_PASSWORD", "SERVICENOW_CLIENT_ID", "SERVICENOW_CLIENT_SECRET"):
    f, e = vals.get(k), os.environ.get(k)
    shown = "<not set>" if not f else (f"<{len(f)} chars>" if ("PASSWORD" in k or "SECRET" in k) else f)
    flag = "   <-- shell value differs from .env" if (e and f and e != f) else ""
    print(f"{k:26}{shown}{flag}")
if not instance.startswith("https://"):
    sys.exit("! SERVICENOW_INSTANCE must start with https://")
if mode not in ("oauth", "basic"):
    sys.exit("! SERVICENOW_AUTH_MODE must be exactly 'oauth' or 'basic'")

headers, auth = {"Accept": "application/json"}, None

if mode == "oauth":
    cid, secret = g("SERVICENOW_CLIENT_ID"), g("SERVICENOW_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("! SERVICENOW_CLIENT_ID / SERVICENOW_CLIENT_SECRET missing in .env")
    print("\n== 2. Requesting a token (grant_type=client_credentials) ==")
    r = httpx.post(f"{instance}/oauth_token.do", headers={"Accept": "application/json"}, timeout=30,
                   data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
    print("HTTP", r.status_code)
    try:
        body = r.json()
    except ValueError:
        body = {}
        print(r.text[:300])
    safe = {k: (f"<{len(str(v))} chars>" if "token" in k and k != "token_type" else v) for k, v in body.items()}
    print(safe)
    if r.status_code != 200 or "access_token" not in body:
        err = str(body).lower()
        print("\n>> Token request FAILED. Likely cause:")
        if "invalid_client" in err or r.status_code == 401:
            print("   Wrong client ID/secret (re-copy the secret with the copy button), or the app is inactive.")
        else:
            print("   The client-credentials grant is not enabled or no OAuth Application User is set.")
            print("   1) System Properties: set glide.oauth.inbound.client.credential.grant_type.enabled = true")
            print("   2) Reopen the Application Registry record and set 'OAuth Application User' (e.g. admin on a PDI)")
        sys.exit(1)
    headers["Authorization"] = f"Bearer {body['access_token']}"
    print("Token OK.")
else:
    auth = (g("SERVICENOW_USERNAME"), g("SERVICENOW_PASSWORD"))
    print("\n== 2. Basic auth ==")

print("\n== 3. Test API call (incident table) ==")
r = httpx.get(f"{instance}/api/now/table/incident", params={"sysparm_limit": 1, "sysparm_fields": "number"},
              headers=headers, auth=auth, timeout=30)
print("HTTP", r.status_code, "|", r.headers.get("content-type", ""))
print(r.text[:300].replace("\n", " "))
hints = {
    200: "Everything works. Restart the MCP server (it reads .env only at startup) and run the tests.",
    401: "Token/credentials rejected. For OAuth try Scope Restriction = 'Broadly scoped'; for basic re-check the password.",
    403: "Authenticated, but the user lacks roles/ACL (the OAuth Application User needs e.g. itil/admin).",
    404: "Wrong instance URL.",
}
print(">>", hints.get(r.status_code, "Unexpected status. Is the instance awake (developer instances hibernate)?"))
if "text/html" in r.headers.get("content-type", ""):
    print(">> HTML response: the instance is probably hibernating or the URL is wrong.")
