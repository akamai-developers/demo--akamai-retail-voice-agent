"""Install private audit tools. The GPU runtime does not import these tools."""

import hashlib
import io
import os
import platform
import tarfile
import urllib.request
import venv

from demo import LOCAL, ROOT, ensure_pip, run

TRIVY_VERSION = "0.76.2"
TRIVY_SHA256 = "bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf"


def ensure_auditor():
    folder = ROOT / ".security-venv"
    python = folder / "bin/python"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(folder)
    ensure_pip(python)
    run(python, "-m", "pip", "install", "pip-audit==2.10.1")


def ensure():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("GPU audit tool setup requires Linux x86_64")
    ensure_auditor()
    tools = LOCAL / "tools"
    tools.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = tools / "trivy"
    if target.is_symlink():
        raise ValueError("Refusing a symlink for the audit tool")
    url = (
        f"https://github.com/aquasecurity/trivy/releases/download/v{TRIVY_VERSION}/"
        f"trivy_{TRIVY_VERSION}_Linux-64bit.tar.gz"
    )
    with urllib.request.urlopen(url, timeout=90) as response:
        archive = response.read(150 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != TRIVY_SHA256:
        raise ValueError("Trivy download checksum did not match")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        member = source.getmember("trivy")
        if not member.isfile():
            raise ValueError("Trivy archive has no regular executable")
        stream = source.extractfile(member)
        if stream is None:
            raise ValueError("Trivy archive is unreadable")
        content = stream.read()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o700)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
    target.chmod(0o700)


if __name__ == "__main__":
    ensure()
