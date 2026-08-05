# -*- coding: utf-8 -*-
"""NordVPN WireGuard proxy configuration.

Thay thế NordVPN CLI bằng cách dùng trực tiếp WireGuard .conf của NordVPN
thông qua wireproxy — mỗi Roxy profile nhận một SOCKS5 proxy riêng biệt,
không cần serialize workers và không cần NordVPN application đang chạy.

Cách lấy file .conf:
    https://my.nordaccount.com/dashboard/nordvpn/manual-configuration/
    → chọn WireGuard → tải về các server cần dùng.

Cách dùng wireproxy:
    https://github.com/pufferffish/wireproxy
    Windows: wireproxy.exe không cần admin, dùng userspace WireGuard.
    Đặt wireproxy.exe vào PATH hoặc điền đường dẫn vào NORDVPN_WG_WIREPROXY_EXE.
"""
from config.env_loader import apply_env_overrides

# Bật/tắt toàn bộ tính năng WireGuard proxy
NORDVPN_WG_ENABLED: bool = False

# Thư mục chứa các file .conf của NordVPN WireGuard
# Ví dụ: C:\nordvpn-wg-configs  → chứa us9999.nordvpn.com.conf, jp123.nordvpn.com.conf, ...
NORDVPN_WG_CONFIGS_DIR: str = ""

# Đường dẫn đến wireproxy.exe (có thể là tên nếu đã có trong PATH)
NORDVPN_WG_WIREPROXY_EXE: str = "wireproxy.exe"
# Không tìm thấy trên PATH thì tải release đã pin + verify SHA-256 vào data/tools
NORDVPN_WG_AUTO_DOWNLOAD: bool = True

# Dải cổng SOCKS5 cục bộ phân bổ cho các proxy đồng thời
# Mỗi registration worker chiếm 1 port trong dải này
NORDVPN_WG_PORT_START: int = 25000
NORDVPN_WG_PORT_END: int = 25099

# Thời gian chờ tối đa (giây) để wireproxy mở cổng SOCKS5
NORDVPN_WG_CONNECT_TIMEOUT: float = 15.0

# Lọc quốc gia khi chọn ngẫu nhiên file .conf
# Ví dụ: "us" → chỉ chọn file có "us" trong tên; để trống = tất cả server
NORDVPN_WG_COUNTRY_FILTER: str = ""

apply_env_overrides(
    globals(),
    {
        "NORDVPN_WG_ENABLED": "bool",
        "NORDVPN_WG_CONFIGS_DIR": "str",
        "NORDVPN_WG_WIREPROXY_EXE": "str",
        "NORDVPN_WG_AUTO_DOWNLOAD": "bool",
        "NORDVPN_WG_PORT_START": "int",
        "NORDVPN_WG_PORT_END": "int",
        "NORDVPN_WG_CONNECT_TIMEOUT": "float",
        "NORDVPN_WG_COUNTRY_FILTER": "str",
    },
)
