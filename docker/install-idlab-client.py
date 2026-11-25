"""Install the IDLab wheel from an official GitHub release."""
import json
import re
import subprocess
import sys
from urllib.request import Request, urlopen

repo = "iobewi/idlab"
version = sys.argv[1]
if version != "latest" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("Expected latest or a numeric release version")
endpoint = "latest" if version == "latest" else f"tags/python-client-v{version}"
request = Request(f"https://api.github.com/repos/{repo}/releases/{endpoint}",
                  headers={"Accept": "application/vnd.github+json"})
with urlopen(request, timeout=30) as response:
    release = json.load(response)
tag = release["tag_name"]
match = re.fullmatch(r"python-client-v([0-9]+\.[0-9]+\.[0-9]+)", tag)
if not match or release["draft"] or release["prerelease"]:
    raise SystemExit("Expected a published stable IDLab client release")
resolved = match.group(1)
if version != "latest" and version != resolved:
    raise SystemExit("Release version mismatch")
name = f"idlab_client-{resolved}-py3-none-any.whl"
assets = [a for a in release["assets"] if a["name"] == name]
if len(assets) != 1:
    raise SystemExit("Expected exactly one matching wheel")
asset = assets[0]
url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
if asset["browser_download_url"] != url:
    raise SystemExit("Unexpected wheel URL")
digest = asset.get("digest") or ""
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    raise SystemExit("Missing or invalid release asset SHA-256")
print(f"Installing IDLab client {resolved}", flush=True)
subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                url + "#sha256=" + digest.split(":", 1)[1]], check=True)
