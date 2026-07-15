"""Constants for the Nuvo Multi-zone Amplifier Media Player component."""

DOMAIN = "legrand_digital_audio"
SOCKET_TIMEOUT = 60

# The Legrand Digital Audio distribution module (AU7000) always exposes its
# JSON control API on this fixed TCP port.
DEFAULT_PORT = 2112

# IEEE MAC OUI registered to "Legrand Home Systems, Inc" (the AU7000 uses this).
# Used for DHCP-based discovery matching.
LEGRAND_OUI = "0026EC"

# ---------------------------------------------------------------------------
# Device types. A config entry represents exactly one physical device; the
# type decides which control path is used during setup.
#   - AU7000: distribution module, TCP/JSON control on port 2112 (connection.py)
#   - AU7001: streaming input module ("NuVo Zone"), UPnP/SOAP control (upnp.py)
# Existing entries created before this field default to AU7000.
# ---------------------------------------------------------------------------
CONF_DEVICE_TYPE = "device_type"
DEVICE_TYPE_AU7000 = "au7000"
DEVICE_TYPE_AU7001 = "au7001"

# SSDP device type advertised by the AU7001 streaming module (NuVo Zone).
NUVO_ZONE_DEVICE_TYPE = "urn:schemas-nuvotechnologies-com:device:Zone:1"

# UPnP service types on the AU7001.
UPNP_SERVICE_AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
UPNP_SERVICE_RENDERING = "urn:schemas-upnp-org:service:RenderingControl:1"
UPNP_SERVICE_CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
NUVO_SERVICE_ZONE = "urn:schemas-nuvotechnologies-com:service:Zone:1"

# Root of the AU7001 on-device music/services menu (Browse2 ObjectID).
NUVO_BROWSE_ROOT = "/nuvo/musicAddService"

# The AU7001 uses standard UPnP RenderingControl volume, scaled 0..100.
UPNP_MAX_VOLUME = 100

# Default names shown in the device registry, entity list, and config entry titles.
DEFAULT_DEVICE_NAME_AU7000 = "Legrand Distribution Module"
DEFAULT_DEVICE_NAME_AU7001 = "Legrand Digital Audio Module"
DEFAULT_ENTRY_TITLE_AU7000 = "Legrand Distribution Module (AU7000)"
DEFAULT_ENTRY_TITLE_AU7001 = "Legrand Digital Audio Module (AU7001)"