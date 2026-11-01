# Model połączeń z chmurą i SaaS: dane przykładowe

Gałąź `model/cloud-saas`, instancja `http://localhost:8100` (token `06438eb2-8019-4776-878c-0941b1f1d1ec`), gałąź Infrahub `main`.
Dane są napisane ręcznie: żaden generator, transform ani check nie działa. Ładują się na samych danych bootstrap
(nie zależą od `30_all`).

## Mapa obiektów

| Rodzaj | Rola | Przykład |
| ------ | ---- | -------- |
| `TopologyProviderConnection` | żądanie usługi "połącz A z B"; jedyny obiekt, który wiąże jedną nogę | `PC-C900-AWS-DX-PROD` |
| `TopologyCarrierCircuit` | fizyka: przewód, cross-connect, internet | `XC-DC90-AWS-DX1` |
| `TopologySessionCircuit` | wirtualne: handoff / vpn / tunnel | `SC-C900-AWS-DX-PROD` |
| `CloudDirectConnect` | port lub połączenie hostowane po stronie chmury | `dx-c900-fra-1`, `er-c900-weu-pri` |
| `CloudVirtualInterface` | VLAN na nim: private / public / transit | `vif-c900-aws-transit` |
| `CloudVPNConnection` | VPN site-to-site | `vpn-c900-aws-euc1` |
| `TopologySaasConnection` | strona SaaS jednej nogi | `saas-c900-webex-fra`, `saas-c900-zscaler-muc` |

Wszystkie krawędzie z połączenia są jednostronne: `locations`, `carrier_circuits`, `session_circuits`,
`cloud_direct_connect`, `cloud_virtual_interfaces`, `cloud_vpn_connection`, `saas_connection`, a także `ip_pool`
(`Handoff-PROD-IPv4`) i `asn_pool` (`PRIVATE-ASN32`). Nic nie wskazuje z powrotem na połączenie.

Koniec obwodu, który jest obiektem, a nie interfejsem, to jednostronna krawędź obwodu `connector_end`
(`capabilities` obwodu zostają interfejsami, jak w HEAD). Z łącznika nie ma krawędzi powrotnej: idziemy od obwodu.

Nogi: (a) AWS Direct Connect (dedykowany, VIF transit do TGW), (b) Azure ExpressRoute: dwie nogi, primary i secondary,
(c) VPN do AWS przez internetowy obwód nośnika, (d) Webex: prywatny peering, (e) Zscaler: tunel przez internet bez BGP.
Nogi (c) i (e) jadą na tym samym obwodzie internetowym `INET-DC90-ISP-DEMO`.

## Pule

Plik obiektów nie umie pobierać z puli. Połączenie wskazuje pule (`ip_pool`, `asn_pool`), a przydzielone wartości
są wpisane na sztywno w miejscu, gdzie przyszły generator je zapisze: nasz adres to podinterfejs sesji
(`06_dc_routing.yml`, /31 z `Handoff-PROD-IPv4`), nasz AS to `local_as` procesu BGP, adres i AS dostawcy to
`peer_ip` i `peer_asn` łącznika (`10_connectors.yml`).

## Ładowanie od zera

Dane ładuje się raz na czystym stosie (zagnieżdżony `segment_deployments` nie jest idempotentny przy ponownym
załadowaniu). Stos z poprawnym schematem i bootstrapem:

```bash
uv run invoke clean-testcontainers
INFRAHUB_TESTING_KEEP_STACK=1 uv run invoke test-integration --tests "tests/integration/test_01_setup.py"
export INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=06438eb2-8019-4776-878c-0941b1f1d1ec
uv run infrahubctl object load data/demos/cloud_saas_model/
```

Nie ładować `data/demos/30_all/08_interconnects/05_provider_connections/`: używa usuniętych pól połączenia.

## Przejście po grafie

```bash
INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=06438eb2-8019-4776-878c-0941b1f1d1ec \
  uv run python data/demos/cloud_saas_model/trace.py c900-demo-web-p c900-demo-subnet-p
```

`trace.py` czyta graf hurtowo (po jednym zapytaniu GraphQL na rodzaj) i idzie po nim w pamięci. Wynik z instancji 8100:

```text
=== L1 (physical): c900-demo-web-p -> c900-demo-subnet-p
  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/2]  (L2/L3 inside the device, no cable)
  --carrier INET-DC90-ISP-DEMO (internet): EXT-ISP-DEMO-PE1:et-0/0/0 <-> dc90-edge-01:Ethernet1/2
  == provider connection PC-C900-AWS-VPN-PROD (vpn, -) locations ['DC90', 'eu-central-1']
  --> VPN vpn-c900-aws-euc1 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/3]  (L2/L3 inside the device, no cable)
  --carrier XC-DC90-AWS-DX1 (cross_connect): dc90-edge-01:Ethernet1/3 <-> DirectConnect dx-c900-fra-1
  == provider connection PC-C900-AWS-DX-PROD (direct_connect, single) locations ['DC90', 'eu-central-1']
  --> VIF vif-c900-aws-transit --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod   [account c900-aws-prod]
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

=== L3 (routed): c900-demo-web-p -> c900-demo-subnet-p
  segment c900-demo-web-p: gateway 10.90.1.1/24 prefix 10.90.1.0/24
  --> SVI dc90-leaf-01:Vlan100 (VLAN 100) in VRF dc90-vrf-prod [PROD]  (deployment DC90)
      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-DX (regular) via dc90-edge-01:Ethernet1/3.1000-->
      --session circuit SC-C900-AWS-DX-PROD (handoff): ends ['dc90-edge-01:Ethernet1/3.1000', 'VirtualInterface vif-c900-aws-transit transit vlan 1000']
      --> VIF vif-c900-aws-transit (provider 10.255.128.1/31 AS64512) --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      == provider connection PC-C900-AWS-DX-PROD
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> instance c900-app-euc1-1 10.200.1.11/24

      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-VPN (regular) via dc90-edge-01:Tunnel1-->
      --session circuit SC-C900-AWS-VPN-1 (vpn): ends ['dc90-edge-01:Tunnel1', 'VPNConnection vpn-c900-aws-euc1']
      --> VPN vpn-c900-aws-euc1 (provider 169.254.10.1/30 AS64512) --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      == provider connection PC-C900-AWS-VPN-PROD
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> instance c900-app-euc1-1 10.200.1.11/24

=== provider connections (all legs, one-sided links)
  PC-C900-AWS-DX-PROD [direct_connect] pools: ['Handoff-PROD-IPv4', 'PRIVATE-ASN32']
      carrier_circuits: ['XC-DC90-AWS-DX1']
      session_circuits: ['SC-C900-AWS-DX-PROD']
      cloud_direct_connect: ['dx-c900-fra-1']
      cloud_virtual_interfaces: ['vif-c900-aws-transit']
  PC-C900-AWS-VPN-PROD [vpn] pools: ['Handoff-PROD-IPv4']
      carrier_circuits: ['INET-DC90-ISP-DEMO']
      session_circuits: ['SC-C900-AWS-VPN-1']
      cloud_vpn_connection: ['vpn-c900-aws-euc1']
  PC-C900-AZ-ER-PRI [hosted] pools: ['Handoff-PROD-IPv4']
      carrier_circuits: ['VC-DC90-AZ-ER-PRI']
      session_circuits: ['SC-C900-AZ-ER-PRI']
      cloud_direct_connect: ['er-c900-weu-pri']
      cloud_virtual_interfaces: ['vif-c900-azure-pri']
  PC-C900-AZ-ER-SEC [hosted] pools: ['Handoff-PROD-IPv4']
      carrier_circuits: ['VC-DC90-AZ-ER-SEC']
      session_circuits: ['SC-C900-AZ-ER-SEC']
      cloud_direct_connect: ['er-c900-weu-sec']
      cloud_virtual_interfaces: ['vif-c900-azure-sec']
  PC-C900-WEBEX-PROD [saas_private] pools: ['Handoff-PROD-IPv4']
      carrier_circuits: ['XC-DC90-WEBEX-1']
      session_circuits: ['SC-C900-WEBEX-PROD']
      saas_connection: ['saas-c900-webex-fra']
  PC-C900-ZSCALER-INET [saas_internet] pools: ['Handoff-PROD-IPv4']
      carrier_circuits: ['INET-DC90-ISP-DEMO']
      session_circuits: ['SC-C900-ZSCALER-TUN1']
      saas_connection: ['saas-c900-zscaler-muc']
```

## Czego model nie wyraża (szczerze)

- L1: między portem `Ethernet1/13` krawędzi (koniec panelu) a portem nośnika (`Ethernet1/3`, `Ethernet1/2`) nie ma kabla:
  to przejście wewnątrz urządzenia, skrypt je tak oznacza.
- Kabel idzie od portu segmentu liścia do panelu krosowego, a nośnik zaczyna się na innym porcie krawędzi. Fizycznie
  spójna ścieżka wymagałaby dodatkowych urządzeń.
- Nie ma serwera w DC: pierwszy hop to port liścia z krawędzią `segments`.
- Strona dostawcy peeringu nie ma procesu BGP: peering ma jeden proces (nasz), druga strona to `peer_ip` i `peer_asn`
  łącznika, a łącznik jest znajdowany przez obwód sesji, który ma ten sam podinterfejs (krawędź `capabilities` peeringu
  jest typu `DcimInterface`, nie wskazuje na łączniki).
- Nogi Azure, Webex i Zscaler mają pełne łańcuchy, ale nie dochodzą do segmentu AWS (inna sieć wirtualna), więc
  `trace.py` ich nie drukuje; widać je w sekcji `provider connections`.
- Segment VXLAN ma prefiks w `gateway.ip_prefix`, segment chmurowy w `cidr_block` (tekst): skrypt traktuje je różnie.
- Krawędź `segments` na porcie jest jednostronna (segment nie ma listy portów): skrypt czyta porty i filtruje po segmencie.
