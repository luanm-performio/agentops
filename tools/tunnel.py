import logging
import socket
import subprocess
import time
from dataclasses import dataclass

from .db_config import DevBox

logging.basicConfig(level=logging.INFO)

WARP_CLI = "/Applications/Cloudflare WARP.app/Contents/Resources/warp-cli"

logger = logging.getLogger(__name__)


@dataclass
class LocalAddress:
    port: int
    host: str = "127.0.0.1"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class SSHTunnel:
    def __init__(self, dev_box: DevBox, remote_host: str, remote_port: int):
        self.dev_box = dev_box
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = get_free_port()
        self.proc = None

    def start(self) -> LocalAddress:
        if not self.dev_box:
            raise ValueError("DevBox configuration is required to start SSH tunnel")

        logger.info(
            f"Starting SSH tunnel from {self.dev_box.username}@{self.dev_box.jumpbox}:22 "
            f"to {self.remote_host}:{self.remote_port}"
        )

        self.proc = subprocess.Popen(
            [
                "ssh",
                f"{self.dev_box.username}@{self.dev_box.jumpbox}",
                "-L",
                f"{self.local_port}:{self.remote_host}:{self.remote_port}",
                "-N",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        time.sleep(1)

        if self.proc.poll() is not None:
            logger.error(self.proc.stderr.read())
            raise RuntimeError("SSH tunnel failed to start")

        return LocalAddress(self.local_port)

    def stop(self) -> None:
        logger.info("Stopping SSH tunnel")
        if self.proc:
            self.proc.terminate()
            self.proc.wait()


def _warp_is_connected() -> bool:
    result = subprocess.run(
        [WARP_CLI, "status"],
        capture_output=True,
        text=True,
    )
    output = result.stdout
    return "Connected" in output and "healthy" in output


def _current_vnet_id() -> str | None:
    import re

    result = subprocess.run(
        [WARP_CLI, "vnet"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if "active" in line.lower() or "current" in line.lower():
            match = re.search(
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                line,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)
    return None


def _probe_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def switch_vnet(
    virtual_network_id: str,
    probe_host: str | None = None,
    probe_port: int = 3306,
    timeout: float = 60.0,
    interval: float = 1.0,
) -> None:
    if _warp_is_connected() and _current_vnet_id() == virtual_network_id:
        logger.info(f"Already on VNet {virtual_network_id}, skipping switch")
        return

    logger.info(f"Switching WARP virtual network to {virtual_network_id}")
    result = subprocess.run(
        [WARP_CLI, "vnet", virtual_network_id],
        capture_output=True,
        text=True,
    )
    logger.info(result)
    deadline = time.monotonic() + timeout
    backoff = interval
    while time.monotonic() < deadline:
        if result.returncode != 0:
            logger.warning(
                f"warp-cli vnet transient error, retrying in {backoff:.1f}s: {result.stderr.strip() or result.stdout.strip()}"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
            result = subprocess.run(
                [WARP_CLI, "vnet", virtual_network_id],
                capture_output=True,
                text=True,
            )
            logger.info(result)
            continue

        if _warp_is_connected():
            if probe_host is None or _probe_tcp(probe_host, probe_port):
                logger.info("WARP connected and healthy")
                return
            logger.debug(
                f"WARP connected but {probe_host}:{probe_port} not reachable yet"
            )
        time.sleep(interval)

    raise Exception(f"WARP did not reach Connected/healthy state within {timeout}s")
