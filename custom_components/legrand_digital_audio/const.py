"""Constants for the Nuvo Multi-zone Amplifier Media Player component."""

DOMAIN = "legrand_digital_audio"
SOCKET_TIMEOUT = 60

# The Legrand Digital Audio distribution module (AU7000) always exposes its
# JSON control API on this fixed TCP port.
DEFAULT_PORT = 2112

# IEEE MAC OUI registered to "Legrand Home Systems, Inc" (the AU7000 uses this).
# Used for DHCP-based discovery matching.
LEGRAND_OUI = "0026EC"