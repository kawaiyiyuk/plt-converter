"""Upload rate-limit IP selection only; no Flask or application initialization."""
import ipaddress


def _address(value):
    # Scoped IPv6 addresses are interface-dependent, not portable limiter keys.
    if not isinstance(value, str) or '%' in value:
        raise ValueError('invalid IP address')
    address = ipaddress.ip_address(value.strip())
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


def parse_trusted_proxy_cidrs(value):
    """Parse explicit peers; reject malformed entries and whole-address-space trust."""
    if not isinstance(value, str):
        raise ValueError('PROJECTOR_TRUSTED_PROXY_CIDRS must be an IP/CIDR list (values redacted)')
    if not value.strip():
        return ()
    networks = []
    try:
        for item in value.split(','):
            if not item.strip() or '%' in item:
                raise ValueError()
            network = ipaddress.ip_network(item.strip(), strict=True)
            if isinstance(network, ipaddress.IPv6Network) and network.network_address.ipv4_mapped is not None:
                # Normalize mapped networks so mapped and native IPv4 peers agree.
                network = ipaddress.ip_network((network.network_address.ipv4_mapped, network.prefixlen - 96))
            if network.prefixlen == 0:
                raise ValueError()
            networks.append(network)
        # Check the union, not only individual entries: two /1 networks (or
        # smaller partitions) must not silently recreate unrestricted /0 trust.
        collapsed = []
        for version in (4, 6):
            family = ipaddress.collapse_addresses(network for network in networks if network.version == version)
            for network in family:
                if network.prefixlen == 0:
                    raise ValueError()
                collapsed.append(network)
    except (ValueError, TypeError):
        raise ValueError('Invalid or unrestricted PROJECTOR_TRUSTED_PROXY_CIDRS (values redacted)') from None
    return tuple(collapsed)


def upload_rate_limit_ip(remote_addr, forwarded_for, trusted_proxy_cidrs):
    """Trust a single valid XFF only when the actual connection peer is allowed."""
    fallback = remote_addr or ''
    try:
        peer = _address(fallback)
    except ValueError:
        return fallback
    fallback = str(peer)
    if not any(peer.version == network.version and peer in network for network in trusted_proxy_cidrs):
        return fallback
    try:
        # ip_address rejects comma-separated chains, ports and non-IP names.
        return str(_address(forwarded_for))
    except ValueError:
        return fallback
