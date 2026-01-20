# On-Prem Load Balancer Demo

This demo shows a complete on-premises HAProxy load balancer setup with the new VIP Service architecture.

## Architecture

```
ManagedLoadBalancer (demo-dc1-haproxy-lb)
  ├─> Frontend Servers: [demo-haproxy-01, demo-haproxy-02]
  └─> VIP Services:
      ├─> www.demo.local:443 (HTTPS)
      │   ├─> Health Check: http-3-3-2000
      │   ├─> VIP IP: 10.10.1.10
      │   └─> Backend Pool: demo-web-pool
      │       └─> Servers: [demo-web-01, demo-web-02]
      ├─> www.demo.local:80 (HTTP redirect)
      │   ├─> Health Check: http-3-3-2000
      │   ├─> VIP IP: 10.10.1.10
      │   └─> Backend Pool: demo-web-pool
      └─> app.demo.local:8080 (TCP)
          ├─> Health Check: tcp-2-2-1000
          ├─> VIP IP: 10.10.1.11
          └─> Backend Pool: demo-app-pool
              └─> Servers: [demo-app-01, demo-app-02]
```

## Load Order

1. `01_deployment.yml` - Demo data center
2. `02_ip_prefixes.yml` - IP address prefixes
3. `03_ip_addresses.yml` - VIP and backend server IPs
4. `04_haproxy_devices.yml` - Frontend HAProxy devices
5. `05_backend_servers.yml` - Backend servers
6. `06_health_checks.yml` - Health check definitions
7. `07_load_balancer.yml` - Load balancer container
8. `08_vip_services.yml` - VIP services (listeners)
9. `09_backend_pools.yml` - Backend pools

## Load Command

```bash
uv run infrahubctl object load data/demos/Managed-lb-example --branch test
```
