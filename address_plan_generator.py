from __future__ import annotations

import ipaddress
from typing import Any


MAX_SITES = 20
MAX_GROUPS_PER_SITE = 20
MAX_TOTAL_SUBNETS = 2000
MAX_K2_VPCS = 100
_GATEWAY_MODES = {"none", "first", "last"}
_K2_APPLIANCE_LABELS = {
    "firewall": "Firewall",
    "s2s_vpn": "S2S VPN",
    "ravpn": "RA VPN",
}


def _required_text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"Укажите {label}")
    return result


def _positive_int(value: Any, label: str, maximum: int) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} должно быть целым числом") from exc
    if result < 1 or result > maximum:
        raise ValueError(f"{label} должно быть от 1 до {maximum}")
    return result


def _prefix(value: Any) -> int:
    raw = str(value or "").strip().removeprefix("/")
    try:
        prefix = int(raw)
    except ValueError as exc:
        raise ValueError("Маска подсети должна быть числом от /1 до /32") from exc
    if prefix < 1 or prefix > 32:
        raise ValueError("Маска подсети должна быть числом от /1 до /32")
    return prefix


def _network(value: Any, label: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError as exc:
        raise ValueError(f"Некорректная {label}") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError(f"{label} должна быть IPv4-сетью")
    return network


def _gateway(network: ipaddress.IPv4Network, mode: str) -> str:
    if mode == "none":
        return ""
    if network.prefixlen >= 31:
        raise ValueError(
            f"Для {network} нельзя автоматически назначить шлюз; выберите «Без шлюза»"
        )
    if mode == "first":
        return str(network.network_address + 1)
    return str(network.broadcast_address - 1)


def _k2_subnet(
    network: ipaddress.IPv4Network,
    vpc: str,
    zone: str,
    site: str,
    description: str,
) -> dict[str, Any]:
    return {
        "cidr": str(network),
        "gateway": "",
        "vlan_number": None,
        "vrf": vpc,
        "zone": zone,
        "site": site,
        "description": description,
    }


def _k2_prefix(value: Any, default: int) -> int:
    return _prefix(default if value is None or str(value).strip() == "" else value)


def _k2_zone_layout(
    roles: list[tuple[str, int]], *, minimum_size: int = 1
) -> tuple[list[tuple[int, int, str]], int]:
    """Pack role networks into one aligned zone slot and return offsets."""
    cursor = 0
    allocations: list[tuple[int, int, str]] = []
    for description, prefix in roles:
        size = 1 << (32 - prefix)
        aligned = ((cursor + size - 1) // size) * size
        allocations.append((aligned, prefix, description))
        cursor = aligned + size
    zone_size = max(minimum_size, 1 << (max(cursor, 1) - 1).bit_length())
    if zone_size > (1 << 32):
        raise ValueError("Выбранные маски подсетей K2 Cloud слишком велики")
    return allocations, zone_size


def _k2_container_prefix(zone_size: int, zone_count: int, minimum_size: int = 1) -> int:
    required = max(minimum_size, zone_size * zone_count)
    block_size = 1 << (required - 1).bit_length()
    if block_size > (1 << 32):
        raise ValueError("Выбранные маски подсетей K2 Cloud слишком велики")
    return 32 - (block_size.bit_length() - 1)


def _generate_k2_cloud_plan(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("k2_cloud")
    if not isinstance(raw, dict):
        raise ValueError("Укажите параметры режима K2 Cloud")

    site_name = _required_text(raw.get("name"), "название площадки K2 Cloud")
    supernet = _network(raw.get("supernet"), "суперсеть K2 Cloud")
    raw_zones = raw.get("zones")
    if not isinstance(raw_zones, list) or len(raw_zones) not in {2, 3}:
        raise ValueError("Для K2 Cloud укажите две или три зоны доступности")
    zones = [
        _required_text(value, f"название зоны {index}")
        for index, value in enumerate(raw_zones, start=1)
    ]
    if len({zone.casefold() for zone in zones}) != len(zones):
        raise ValueError("Названия зон K2 Cloud должны различаться")

    raw_workloads = raw.get("workload_vpcs") or []
    raw_appliances = raw.get("appliance_vpcs") or []
    if not isinstance(raw_workloads, list) or not isinstance(raw_appliances, list):
        raise ValueError("Некорректный список VPC")
    total_vpc_count = (
        len(raw_workloads)
        + len(raw_appliances)
        + (1 if raw.get("include_transit_vpc") is True else 0)
    )
    if total_vpc_count > MAX_K2_VPCS:
        raise ValueError(f"Можно создать не более {MAX_K2_VPCS} VPC")

    requests: list[dict[str, Any]] = []
    seen_vpcs: set[str] = set()

    def unique_vpc_name(value: Any) -> str:
        name = _required_text(value, "название VPC")
        normalized = name.casefold()
        if normalized in seen_vpcs:
            raise ValueError(f"Название VPC «{name}» повторяется")
        seen_vpcs.add(normalized)
        return name

    for raw_vpc in raw_workloads:
        if not isinstance(raw_vpc, dict):
            raise ValueError("Некорректные параметры VPC виртуальных машин")
        layout, zone_size = _k2_zone_layout([
            ("подсеть виртуальных машин", _k2_prefix(raw_vpc.get("vm_prefix"), 24)),
            ("транзитная подсеть к TGW", _k2_prefix(raw_vpc.get("tgw_prefix"), 28)),
        ])
        requests.append({
            "prefix": _k2_container_prefix(zone_size, len(zones)),
            "kind": "workload",
            "name": unique_vpc_name(raw_vpc.get("name")),
            "zones": zones,
            "zone_size": zone_size,
            "layout": layout,
        })

    for raw_vpc in raw_appliances:
        if not isinstance(raw_vpc, dict):
            raise ValueError("Некорректные параметры VPC сетевых устройств")
        appliance_type = str(raw_vpc.get("type") or "").strip()
        if appliance_type not in _K2_APPLIANCE_LABELS:
            raise ValueError("Тип VPC должен быть Firewall, S2S VPN или RA VPN")
        zone_scope = str(raw_vpc.get("zone_scope") or "all").strip()
        scoped_zones = {
            "all": zones,
            "primary": zones[:1],
            "secondary": zones[1:2],
            "tertiary": zones[2:3] if len(zones) == 3 else None,
        }.get(zone_scope)
        if not scoped_zones:
            raise ValueError("Некорректный выбор зон для VPC сетевых устройств")
        label = _K2_APPLIANCE_LABELS[appliance_type]
        cluster = raw_vpc.get("cluster") is True
        default_outside_prefix = 25 if appliance_type == "firewall" else 28
        roles = [
            (f"{label} outside", _k2_prefix(
                raw_vpc.get("outside_prefix"), default_outside_prefix
            )),
            (f"{label} inside", _k2_prefix(raw_vpc.get("inside_prefix"), 28)),
        ]
        if cluster:
            roles.append((
                f"{label} interlink", _k2_prefix(raw_vpc.get("interlink_prefix"), 28)
            ))
        roles.append((
            "транзитная подсеть к TGW", _k2_prefix(raw_vpc.get("tgw_prefix"), 28)
        ))
        layout, zone_size = _k2_zone_layout(roles, minimum_size=256)
        if appliance_type == "ravpn":
            user_prefix = _k2_prefix(raw_vpc.get("user_prefix"), 22)
            user_pool_size = 1 << (32 - user_prefix)
            if zone_size * len(scoped_zones) > user_pool_size:
                raise ValueError(
                    "Маска пула пользователей RA VPN слишком мала для выбранных зон и подсетей"
                )
            vpc_prefix = user_prefix
        else:
            vpc_prefix = _k2_container_prefix(zone_size, len(scoped_zones))
        requests.append({
            "prefix": vpc_prefix,
            "kind": "appliance",
            "name": unique_vpc_name(raw_vpc.get("name")),
            "zones": scoped_zones,
            "appliance_type": appliance_type,
            "zone_size": zone_size,
            "layout": layout,
        })

    if raw.get("include_transit_vpc") is True:
        layout, zone_size = _k2_zone_layout([
            ("транзитная сеть между TGW", _k2_prefix(raw.get("transit_prefix"), 24)),
        ])
        requests.append({
            "prefix": _k2_container_prefix(zone_size, len(zones)),
            "kind": "transit",
            "name": unique_vpc_name(raw.get("transit_vpc_name") or "VPC TRANSIT"),
            "zones": zones,
            "zone_size": zone_size,
            "layout": layout,
        })

    if not requests:
        raise ValueError("Добавьте хотя бы один VPC K2 Cloud")

    ordered_requests = sorted(
        enumerate(requests), key=lambda item: (item[1]["prefix"], item[0])
    )
    cursor = int(supernet.network_address)
    limit = int(supernet.broadcast_address) + 1
    generated_subnets: list[dict[str, Any]] = []
    used_addresses = 0

    for _request_index, request in ordered_requests:
        prefix = request["prefix"]
        size = 1 << (32 - prefix)
        aligned = ((cursor + size - 1) // size) * size
        if aligned + size > limit:
            raise ValueError(f"VPC K2 Cloud не помещаются в суперсеть {supernet}")
        block = ipaddress.IPv4Network((aligned, prefix))
        vpc_name = request["name"]
        request_zones = request["zones"]
        segment_description = (
            "Сегмент пользователей RA VPN"
            if request.get("appliance_type") == "ravpn"
            else f"Сегмент {vpc_name}"
        )
        generated_subnets.append(_k2_subnet(
            block, vpc_name, ", ".join(request_zones), site_name,
            segment_description,
        ))

        if request["kind"] == "workload":
            for zone_index, zone in enumerate(request_zones):
                zone_base = int(block.network_address) + zone_index * request["zone_size"]
                for offset, subnet_prefix, description in request["layout"]:
                    network = ipaddress.IPv4Network((zone_base + offset, subnet_prefix))
                    generated_subnets.append(_k2_subnet(
                        network, vpc_name, zone, site_name, f"{zone} - {description}",
                    ))
        elif request["kind"] == "appliance":
            for zone_index, zone in enumerate(request_zones):
                zone_base = int(block.network_address) + zone_index * request["zone_size"]
                for offset, subnet_prefix, description in request["layout"]:
                    network = ipaddress.IPv4Network(
                        (zone_base + offset, subnet_prefix)
                    )
                    generated_subnets.append(_k2_subnet(
                        network, vpc_name, zone, site_name,
                        f"{zone} - {description}",
                    ))
        else:
            for zone_index, zone in enumerate(request_zones):
                zone_base = int(block.network_address) + zone_index * request["zone_size"]
                for offset, subnet_prefix, description in request["layout"]:
                    network = ipaddress.IPv4Network((zone_base + offset, subnet_prefix))
                    generated_subnets.append(_k2_subnet(
                        network, vpc_name, zone, site_name, f"{zone} - {description}",
                    ))

        used_addresses += block.num_addresses
        cursor = aligned + size

    if len(generated_subnets) > MAX_TOTAL_SUBNETS:
        raise ValueError(
            f"За один раз можно сгенерировать не более {MAX_TOTAL_SUBNETS} подсетей"
        )
    site = {
        "name": site_name,
        "cidr": str(supernet),
        "gateway_mode": "none",
        "subnets": generated_subnets,
        "subnet_count": len(generated_subnets),
        "used_addresses": used_addresses,
        "free_addresses": supernet.num_addresses - used_addresses,
    }
    return {
        "mode": "k2_cloud",
        "routing_label": "VPC",
        "sites": [site],
        "total_subnets": len(generated_subnets),
    }


def _generate_standard_plan(payload: dict[str, Any]) -> dict[str, Any]:
    raw_sites = payload.get("sites") if isinstance(payload, dict) else None
    if not isinstance(raw_sites, list) or not raw_sites:
        raise ValueError("Добавьте хотя бы одну площадку")
    if len(raw_sites) > MAX_SITES:
        raise ValueError(f"Можно сгенерировать не более {MAX_SITES} площадок за один раз")

    result_sites: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    site_networks: list[tuple[str, ipaddress.IPv4Network]] = []
    total_subnets = 0

    for site_index, raw_site in enumerate(raw_sites, start=1):
        if not isinstance(raw_site, dict):
            raise ValueError(f"Некорректные параметры площадки {site_index}")
        name = _required_text(raw_site.get("name"), f"название площадки {site_index}")
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise ValueError(f"Название площадки «{name}» повторяется")
        seen_names.add(normalized_name)

        supernet = _network(raw_site.get("supernet"), f"суперсеть площадки «{name}»")
        for other_name, other_network in site_networks:
            if supernet.overlaps(other_network):
                raise ValueError(
                    f"Суперсеть {supernet} площадки «{name}» пересекается с "
                    f"площадкой «{other_name}» ({other_network})"
                )
        site_networks.append((name, supernet))

        gateway_mode = str(raw_site.get("gateway_mode") or "none").strip().lower()
        if gateway_mode not in _GATEWAY_MODES:
            raise ValueError(f"Некорректный режим шлюза площадки «{name}»")

        raw_groups = raw_site.get("groups")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise ValueError(f"Добавьте хотя бы одну маску для площадки «{name}»")
        if len(raw_groups) > MAX_GROUPS_PER_SITE:
            raise ValueError(
                f"Для площадки «{name}» можно задать не более {MAX_GROUPS_PER_SITE} групп"
            )

        requests: list[tuple[int, int, int]] = []
        for group_index, raw_group in enumerate(raw_groups):
            if not isinstance(raw_group, dict):
                raise ValueError(f"Некорректная группа масок площадки «{name}»")
            prefix = _prefix(raw_group.get("prefix"))
            if prefix <= supernet.prefixlen:
                raise ValueError(
                    f"Маска /{prefix} должна быть меньше суперсети {supernet} "
                    f"(число после / должно быть больше {supernet.prefixlen})"
                )
            count = _positive_int(
                raw_group.get("count"), "Количество подсетей", MAX_TOTAL_SUBNETS
            )
            requests.extend((prefix, group_index, ordinal) for ordinal in range(count))

        total_subnets += len(requests)
        if total_subnets > MAX_TOTAL_SUBNETS:
            raise ValueError(
                f"За один раз можно сгенерировать не более {MAX_TOTAL_SUBNETS} подсетей"
            )

        vlan_raw = raw_site.get("vlan_start")
        vlan_start: int | None = None
        if vlan_raw not in (None, ""):
            vlan_start = _positive_int(vlan_raw, "Начальный VLAN", 4094)
            if vlan_start + len(requests) - 1 > 4094:
                raise ValueError(f"Диапазон VLAN площадки «{name}» выходит за пределы 4094")

        vrf = str(raw_site.get("vrf") or "").strip()
        zone = str(raw_site.get("zone") or "").strip()
        description_prefix = str(raw_site.get("description_prefix") or name).strip()
        ordered_requests = sorted(requests, key=lambda item: (item[0], item[1], item[2]))
        cursor = int(supernet.network_address)
        limit = int(supernet.broadcast_address) + 1
        generated_subnets: list[dict[str, Any]] = []

        for allocation_index, (prefix, _group_index, _ordinal) in enumerate(
            ordered_requests, start=1
        ):
            size = 1 << (32 - prefix)
            aligned = ((cursor + size - 1) // size) * size
            if aligned + size > limit:
                raise ValueError(
                    f"Подсети площадки «{name}» не помещаются в суперсеть {supernet}"
                )
            subnet = ipaddress.IPv4Network((aligned, prefix))
            generated_subnets.append(
                {
                    "cidr": str(subnet),
                    "gateway": _gateway(subnet, gateway_mode),
                    "vlan_number": (
                        vlan_start + allocation_index - 1
                        if vlan_start is not None
                        else None
                    ),
                    "vrf": vrf,
                    "zone": zone,
                    "site": name,
                    "description": f"{description_prefix}-{allocation_index:03d}",
                }
            )
            cursor = aligned + size

        used_addresses = sum(1 << (32 - item[0]) for item in ordered_requests)
        result_sites.append(
            {
                "name": name,
                "cidr": str(supernet),
                "gateway_mode": gateway_mode,
                "subnets": generated_subnets,
                "subnet_count": len(generated_subnets),
                "used_addresses": used_addresses,
                "free_addresses": supernet.num_addresses - used_addresses,
            }
        )

    return {
        "mode": "standard",
        "routing_label": "VRF",
        "sites": result_sites,
        "total_subnets": total_subnets,
    }


def generate_address_plan(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Некорректные параметры генератора")
    mode = str(payload.get("mode") or "standard").strip()
    if mode == "k2_cloud":
        return _generate_k2_cloud_plan(payload)
    if mode != "standard":
        raise ValueError("Неизвестный режим генератора адресного плана")
    return _generate_standard_plan(payload)
