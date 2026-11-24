#!/usr/bin/env python3
"""One-time ecobee sign-in for pellet_watch's optional ecobee heat dump.

Run this on your own computer (not the NAS):

    pip install python-ecobee-api
    python ecobee_login.py

It asks for your ecobee.com email and password (typed, never shown, never
saved) and, if your account uses two-step sign-in, the 6-digit code. It then
writes pellet_watch_ecobee_token.json next to this file. Upload that file to
the pellet_watch folder on the NAS (beside pellet_watch.py, NOT the web
folder). It holds a sign-in token, not your password: treat it like a
password anyway. To revoke it, change your ecobee password.

Uses the same sign-in as Home Assistant's ecobee integration (2026.3+),
through the python-ecobee-api library.
"""
import getpass
import json
import os
import sys

try:
    from pyecobee import Ecobee
    from pyecobee.errors import (EcobeeAuthFailedError, EcobeeAuthMfaRequiredError,
                                 EcobeeAuthUnknownError)
except ImportError:
    sys.exit("Install the library first:  pip install python-ecobee-api")

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "pellet_watch_ecobee_token.json")


def main():
    email = input("ecobee.com email: ").strip()
    password = getpass.getpass("ecobee.com password (not shown): ")
    eco = Ecobee(config={"USERNAME": email, "PASSWORD": password})
    try:
        try:
            eco.request_tokens_web()
        except EcobeeAuthMfaRequiredError as e:
            kind = "text message" if e.challenge.mfa_type == "sms" else "authenticator app"
            code = input("Code from your %s: " % kind).strip()
            eco.submit_mfa_code(e.challenge, code)
    except EcobeeAuthFailedError as e:
        sys.exit("ecobee refused the sign-in: %s" % e)
    except EcobeeAuthUnknownError as e:
        sys.exit("Sign-in failed: %s" % e)
    finally:
        password = None
        eco.password = None
        eco.config = None                # the library keeps a copy here
    if not eco.refresh_token:
        sys.exit("Signed in, but ecobee returned no refresh token.")
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": eco.refresh_token}, f)
    os.replace(tmp, OUT)
    print("Saved %s" % OUT)
    print("Upload it to the pellet_watch folder on the NAS, then delete this copy.")


if __name__ == "__main__":
    main()
