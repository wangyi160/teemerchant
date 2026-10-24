"""Platform HTTPS endpoint validation (the former archive downloader was removed)."""
import ipaddress
from urllib.parse import urlsplit




def platform_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('管理平台地址无效')
    if not value:
        return ''
    url = urlsplit(value)
    try:
        loopback = url.hostname == 'localhost' or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = False
    try:
        port = url.port
    except ValueError:
        raise ValueError('管理平台端口无效') from None
    if (not url.hostname or url.username is not None or url.password is not None or url.path not in ('', '/')
            or url.query or url.fragment or (url.scheme != 'https' and not (url.scheme == 'http' and loopback))):
        raise ValueError('请填写管理平台 HTTPS 根地址；本机允许 HTTP')
    return value.rstrip('/')
