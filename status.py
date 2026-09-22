#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Shows the protected device status; sign-in with the device code."""
import getpass, http.cookiejar, json, os, sys, time, urllib.error, urllib.request

def option(argv, name):
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv): return argv[index + 1]
        if value.startswith(name + "="): return value.split("=", 1)[1]

def login(opener, host, code):
    request = urllib.request.Request(host + "/api/session",
        json.dumps({"device_code": code}).encode(), {"Content-Type": "application/json"})
    opener.open(request, timeout=5).read()

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    code_option = option(argv, "--device-code")
    positional, skip = [], False
    for value in argv:
        if skip: skip = False; continue
        if value == "--device-code": skip = True
        elif not value.startswith("--") and value != code_option: positional.append(value)
    host = positional[0] if positional else "192.168.4.1"
    interval = float(positional[1]) if len(positional) > 1 else 1.0
    host = (host if host.startswith("http") else "http://" + host).rstrip("/")
    code = code_option or os.getenv("ESP_RTK_DEVICE_CODE")
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    authenticated = False
    while True:
        try:
            try: response = opener.open(host + "/status", timeout=5)
            except urllib.error.HTTPError as error:
                if error.code != 401 or authenticated: raise
                code = code or getpass.getpass("Device code: ")
                login(opener, host, code); authenticated = True
                response = opener.open(host + "/status", timeout=5)
            data = json.load(response)
            if "--json" in argv: print(json.dumps(data, indent=2), flush=True)
            else:
                fix, system = data["fix"], data["system"]
                print("%s %-12s sats=%s hdop=%s corr=%ss station=%s %s/%s alt=%s age=%ss RAM=%s IP=%s" %
                    (time.strftime("%H:%M:%S"), fix.get("fix_status_text", "?"), fix.get("sats", "?"),
                     fix.get("hdop", "?"), fix.get("correction_age_sec", "-"), fix.get("station_id") or "-",
                     fix.get("lat", "-"), fix.get("lon", "-"), fix.get("alt", "?"),
                     data.get("fix_age_sec", "?"), system.get("ram_free_bytes", "?"), system.get("ip", "?")), flush=True)
        except Exception as error:
            print("%s is unreachable (%s: %s)" %
                  (time.strftime("%H:%M:%S"), type(error).__name__, error), flush=True)
        if "--once" in argv: break
        time.sleep(interval)

if __name__ == "__main__": main()
