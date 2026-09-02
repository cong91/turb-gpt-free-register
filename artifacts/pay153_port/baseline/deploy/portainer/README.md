# OVH Portainer service

Portainer runs as an independent Docker Compose project on OVH. Its HTTPS
listener is published only on the OVH loopback interface:

```text
OVH 127.0.0.1:9443 -> portainer:9443
```

The local operator tunnel files are intentionally kept outside this repository
in `C:\Users\mrc\Documents\Portainer-OVH-Tunnel`.

Portainer's first start presents its initial administrator setup. Use a unique
Portainer password; do not reuse the application WebUI password.

The compose project does not publish ports `8000` or `9000`, does not mount
the application runtime volume, and does not participate in the application
deployment workflow.
