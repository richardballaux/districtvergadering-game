#!/usr/bin/env python3
"""Entry point – start with: python run.py"""
import socket
from app import app

def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    ip = get_local_ip()
    print("\n" + "="*50)
    print("  🦁 Stadsspel Aalst – Leo District")
    print("="*50)
    print(f"  Admin:       http://{ip}:5050/admin")
    print(f"  Deelnemers:  http://{ip}:5050/")
    print(f"  Wachtwoord:  RikkertLeos1")
    print("="*50 + "\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
