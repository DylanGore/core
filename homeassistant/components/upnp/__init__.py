"""Open ports in your router for Home Assistant and provide statistics."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import ssdp
from homeassistant.components.network import async_get_source_ip
from homeassistant.components.network.const import PUBLIC_TARGET_IP
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_LOCAL_IP,
    CONFIG_ENTRY_HOSTNAME,
    CONFIG_ENTRY_ST,
    CONFIG_ENTRY_UDN,
    DOMAIN,
    DOMAIN_CONFIG,
    DOMAIN_DEVICES,
    DOMAIN_LOCAL_IP,
    LOGGER as _LOGGER,
)
from .device import Device

NOTIFICATION_ID = "upnp_notification"
NOTIFICATION_TITLE = "UPnP/IGD Setup"

PLATFORMS = ["sensor"]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_LOCAL_IP): vol.All(ip_address, cv.string),
            },
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType):
    """Set up UPnP component."""
    _LOGGER.debug("async_setup, config: %s", config)
    conf_default = CONFIG_SCHEMA({DOMAIN: {}})[DOMAIN]
    conf = config.get(DOMAIN, conf_default)
    local_ip = await async_get_source_ip(hass, PUBLIC_TARGET_IP)
    hass.data[DOMAIN] = {
        DOMAIN_CONFIG: conf,
        DOMAIN_DEVICES: {},
        DOMAIN_LOCAL_IP: conf.get(CONF_LOCAL_IP, local_ip),
    }

    # Only start if set up via configuration.yaml.
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_IMPORT}
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up UPnP/IGD device from a config entry."""
    _LOGGER.debug("Setting up config entry: %s", entry.unique_id)

    udn = entry.data[CONFIG_ENTRY_UDN]
    st = entry.data[CONFIG_ENTRY_ST]  # pylint: disable=invalid-name
    usn = f"{udn}::{st}"

    # Register device discovered-callback.
    device_discovered_event = asyncio.Event()
    discovery_info: Mapping[str, Any] | None = None

    @callback
    def device_discovered(info: Mapping[str, Any]) -> None:
        nonlocal discovery_info
        _LOGGER.debug(
            "Device discovered: %s, at: %s", usn, info[ssdp.ATTR_SSDP_LOCATION]
        )
        discovery_info = info
        device_discovered_event.set()

    cancel_discovered_callback = ssdp.async_register_callback(
        hass,
        device_discovered,
        {
            "usn": usn,
        },
    )

    try:
        await asyncio.wait_for(device_discovered_event.wait(), timeout=10)
    except asyncio.TimeoutError as err:
        _LOGGER.debug("Device not discovered: %s", usn)
        raise ConfigEntryNotReady from err
    finally:
        cancel_discovered_callback()

    # Create device.
    location = discovery_info[  # pylint: disable=unsubscriptable-object
        ssdp.ATTR_SSDP_LOCATION
    ]
    device = await Device.async_create_device(hass, location)

    # Save device.
    hass.data[DOMAIN][DOMAIN_DEVICES][udn] = device

    # Ensure entry has a unique_id.
    if not entry.unique_id:
        _LOGGER.debug(
            "Setting unique_id: %s, for config_entry: %s",
            device.unique_id,
            entry,
        )
        hass.config_entries.async_update_entry(
            entry=entry,
            unique_id=device.unique_id,
        )

    # Ensure entry has a hostname, for older entries.
    if (
        CONFIG_ENTRY_HOSTNAME not in entry.data
        or entry.data[CONFIG_ENTRY_HOSTNAME] != device.hostname
    ):
        hass.config_entries.async_update_entry(
            entry=entry,
            data={CONFIG_ENTRY_HOSTNAME: device.hostname, **entry.data},
        )

    # Create device registry entry.
    device_registry = await dr.async_get_registry(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_UPNP, device.udn)},
        identifiers={(DOMAIN, device.udn)},
        name=device.name,
        manufacturer=device.manufacturer,
        model=device.model_name,
    )

    # Create sensors.
    _LOGGER.debug("Enabling sensors")
    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    # Start device updater.
    await device.async_start()

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a UPnP/IGD device from a config entry."""
    _LOGGER.debug("Unloading config entry: %s", config_entry.unique_id)

    udn = config_entry.data.get(CONFIG_ENTRY_UDN)
    if udn in hass.data[DOMAIN][DOMAIN_DEVICES]:
        device = hass.data[DOMAIN][DOMAIN_DEVICES][udn]
        await device.async_stop()

        del hass.data[DOMAIN][DOMAIN_DEVICES][udn]

    _LOGGER.debug("Deleting sensors")
    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
