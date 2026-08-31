from __future__ import annotations

import ipaddress
from typing import Any


MAX_SITES = 20
MAX_GROUPS_PER_SITE = 20
MAX_TOTAL_SUBNETS = 2000
_GATEWAY_MODES = {"none", "first", "last"}


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


def generate_address_plan(payload: dict[str, Any]) -> dict[str, Any]:
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
            subnet = ipaddress.ip_network((aligned, prefix))
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

    return {"sites": result_sites, "total_subnets": total_subnets}
