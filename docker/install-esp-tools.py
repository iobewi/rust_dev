"""Install versioned official release binaries after SHA-256 verification."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import urllib.request
import zipfile


def main():
    arch = platform.machine()
    if arch not in {'x86_64', 'aarch64'}:
        raise SystemExit(f'Unsupported ESP host architecture: {arch}')
    manifest = json.loads(Path(__file__).with_name('esp-tools.json').read_text())
    destination = Path(os.environ['CARGO_HOME']) / 'bin'
    destination.mkdir(parents=True, exist_ok=True)
    for tool, entry in manifest.items():
        archive = entry['archives'][arch]
        url = f"https://github.com/{entry['repository']}/releases/download/v{entry['version']}/{archive['file']}"
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read(128 * 1024 * 1024 + 1)
        if len(data) > 128 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != archive['sha256']:
            raise SystemExit(f'Invalid release digest or size for {tool}')
        if archive['file'].endswith('.zip'):
            # Read only the expected executable, never extract arbitrary archive paths.
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                files = [name for name in bundle.namelist() if Path(name).name == tool]
                if len(files) != 1 or bundle.getinfo(files[0]).file_size > 128 * 1024 * 1024:
                    raise SystemExit(f'Invalid release archive for {tool}')
                data = bundle.read(files[0])
        binary = destination / tool
        binary.write_bytes(data)
        binary.chmod(0o755)
        print(f'Installed {tool} {entry["version"]} ({arch}, verified SHA-256)', flush=True)


if __name__ == '__main__':
    main()
