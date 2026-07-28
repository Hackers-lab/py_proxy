import os
import re
import shutil
import subprocess
import sys

def get_version() -> str:
    with open(os.path.join("nst", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
        return m.group(1) if m else "0.0.0"

def build():
    ver = get_version()
    # Format version string as X.Y.Z.0 for PE metadata if needed
    ver_parts = ver.split(".")
    while len(ver_parts) < 4:
        ver_parts.append("0")
    ver_4 = ".".join(ver_parts[:4])

    print(f"Building NetSplitTunnel v{ver} with Nuitka C++ Compiler...")

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--windows-disable-console",
        "--enable-plugin=pyqt6",
        "--output-filename=NetSplitTunnel.exe",
        f"--company-name=Pramod Verma",
        f"--product-name=Net Split-Tunneler",
        f"--file-version={ver_4}",
        f"--product-version={ver_4}",
        f"--file-description=Net Split-Tunneler & Proxy Sharing Tool",
        f"--copyright=Copyright (C) 2026 Pramod Verma",
        "--windows-icon-from-ico=icon.ico",
        "--include-data-files=icon.ico=icon.ico",
        "--include-data-files=icon.png=icon.png",
        "--include-package=nst",
        "--assume-yes-for-downloads",
        "net_tunnel.py",
    ]

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    # Sync net_tunnel.dist into dist/NetSplitTunnel for Inno Setup
    src_dist = "net_tunnel.dist"
    target_dist = os.path.join("dist", "NetSplitTunnel")

    if os.path.exists(src_dist):
        if os.path.exists(target_dist):
            shutil.rmtree(target_dist)
        os.makedirs(os.path.dirname(target_dist), exist_ok=True)
        shutil.copytree(src_dist, target_dist)
        print(f"Copied {src_dist} -> {target_dist}")

    print("Nuitka build complete.")

if __name__ == "__main__":
    build()
