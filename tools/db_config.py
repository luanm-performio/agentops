from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class DevBox:
    ca_file: str
    ssh_pkey: str
    jumpbox: str
    username: str


@dataclass(frozen=True)
class Region:
    remote_bind_address: str
    username: str
    password: str
    remote_bind_port: int = 3306
    devbox_required: bool = False
    devbox: DevBox | None = None
    virtual_network_id: str | None = None
    name: str | None = None


def load_config(config_path: str) -> list[Region]:
    with open(config_path, "r") as f:
        config_dict = yaml.safe_load(f)

    regions = []
    for region_dict in config_dict["regions"]:
        if region_dict.get("devbox_required", False):
            devbox_dict = config_dict.get("devbox", {})
            devbox = DevBox(**devbox_dict) if devbox_dict else None
        else:
            devbox = None

        region = Region(
            remote_bind_address=region_dict["remote_bind_address"],
            remote_bind_port=region_dict.get("remote_bind_port", 3306),
            devbox_required=region_dict.get("devbox_required", False),
            devbox=devbox,
            username=region_dict["username"],
            password=region_dict["password"],
            virtual_network_id=region_dict.get("virtual_network_id"),
            name=region_dict.get("name"),
        )

        regions.append(region)

    return regions
