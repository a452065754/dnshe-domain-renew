"""DNSHE 域名查询 / 自动续期工具。"""

from .client import VERSION, DnsheClient, DnsheError, Domain, plan_renewals  # noqa: F401
from .cli import main  # noqa: F401

__version__ = VERSION
