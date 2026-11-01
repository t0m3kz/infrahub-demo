# Model połączeń z chmurą i SaaS: dane przykładowe

Gałąź `model/cloud-saas`, instancja `http://localhost:8100` (token `06438eb2-8019-4776-878c-0941b1f1d1ec`), gałąź Infrahub `main`.
Dane są napisane ręcznie: żaden generator, transform ani check nie działa. Ładują się na samych danych bootstrap
(nie zależą od `30_all`).

## Słownik wspólny dla wszystkich chmur

Operatorzy różnią się słownictwem, nie strukturą. Model ma pięć wspólnych elementów; słowo operatora (VIF, peering,
VLAN attachment, tunel) jest tylko w nazwach obiektów, opisach i w tabeli poniżej.

| Element wspólny | Rodzaj | Co to jest |
| --------------- | ------ | ---------- |
| transport | `CloudDirectConnect` (`physical` / `logical`, `rides_on`), `CloudVPNConnection` (marker `CloudTransport`) | droga do krawędzi chmury: port lub pakiet (`physical`), obwód na porcie partnera albo własnym (`logical`), albo połączenie VPN, które trzyma tunele |
| peering ("interfejs chmury") | `CloudPeeringInterface` | zakończenie L3/BGP po stronie chmury; na nim kończy się obwód sesji (`endpoints`); adres i AS dostawcy to `peer_ip`, `peer_asn` |
| cloud edge gateway | `CloudGateway`, `gateway_type: cloud_edge_gateway` | to, na czym peering ląduje w chmurze, zanim dojdzie do sieci |
| vpn gateway / customer gateway | `CloudGateway`, `vpn_gateway` / `customer_gateway` | brama VPN dostawcy; nasza strona VPN (publiczny adres i AS); `customer_gateway` jest wymagany na połączeniu VPN |
| transit / hub gateway | `CloudTransitGateway` (bez zmian) | hub routingu |
| sieć wirtualna | `CloudVirtualNetwork` (bez zmian) | VPC / VNet |

Nasza strona to zawsze podinterfejs urządzenia i nasz proces BGP; nie ma jej w obiektach chmury.

`CloudPeeringInterface` (HFID: `name`): `scope` (private, public, saas; "transit" wynika z bramy), `routing` (bgp, static),
`vlan_id` albo `tunnel_index`, `mtu`, `bfd`, `service_id`; tunel: `endpoint_ip`, `tunnel_mode` (ipsec, gre),
`crypto_profile` (`SecurityIPsecProfile`), `tunnel_key` (`RoutingPassword`, klucz IPsec; BGP to `auth`); relacje `transport`, `account`, `region`, `saas_region`,
`owner` (SaaS), `gateway`, `transit_gateway`, `virtual_network`, `auth` (`RoutingPassword`), `advertised_prefixes`,
oraz `peer_ip`, `peer_asn` z `TopologyConnectorRouting`.

## Tabela wzorców (blueprint) per operator

`OrganizationProvider.connection_profile` (aws, azure, gcp, ...) mówi, jaki jest wzorzec. Komórka: wspólny element,
w nawiasie słowo operatora.

| Profil i scenariusz | transport | peering | cloud edge / vpn gateway | transit gateway | sieć wirtualna | inne |
| ------------------- | --------- | ------- | ------------------------ | --------------- | -------------- | ---- |
| aws, transit | `CloudDirectConnect` (Direct Connect: `physical` lub `logical`; LAG = pakiet `physical`, członkowie `rides_on`) | scope `private` (transit VIF), tranzyt wynika z bramy | `cloud_edge_gateway` (Direct Connect Gateway) | tak (TGW) | tak (VPC przez załącznik TGW) | prefiksy dozwolone na asocjacji: runda 2 (`allowed_prefixes` załącznika) |
| aws, private | `CloudDirectConnect` | scope `private` (private VIF) | `vpn_gateway` (VGW) albo `cloud_edge_gateway` (DXGW) | nie | tak (VPC) | |
| azure | `CloudDirectConnect` `logical` (obwód ExpressRoute, `service_key`) | DWA peeringi, scope `private` lub `public` (private / Microsoft peering), po /30 | `cloud_edge_gateway` (ExpressRoute Gateway) | nie | tak (VNet) | dwa obwody nośnika na jeden transport |
| gcp | `CloudDirectConnect` (`logical` + `partner_account` dla Partner Interconnect; `physical` dla Dedicated) | scope `private` (VLAN attachment) | `cloud_edge_gateway` (Cloud Router) | nie | tak (VPC) | |
| aws / azure / gcp, VPN | `CloudVPNConnection` (połączenie VPN) | N peeringów (tunele: `tunnel_index`, `endpoint_ip`, `tunnel_mode`, `crypto_profile`, `tunnel_key`), każdy z BGP w /30 | `vpn_gateway` albo transit gateway | opcjonalnie | opcjonalnie | `customer_gateway` (nasz adres i AS) |
| saas | brak, albo transport gdy jest droga fizyczna (Fabric) | scope `saas` (Webex: BGP; Zscaler: `routing: static`, tunel) | brak | brak | brak | `owner`, `saas_region`, `service_id` |

## Mapa obiektów

| Rodzaj | Rola | Przykład |
| ------ | ---- | -------- |
| `TopologyCarrierCircuit` | fizyka: przewód, cross-connect, wirtualne połączenie, internet | `XC-DC90-AWS-DX1`, `INET-DC90-ISP-DEMO` |
| `TopologySessionCircuit` | wirtualne: handoff / vpn / tunnel; jego koniec to peering | `SC-C900-AWS-DX-PROD` |
| `CloudDirectConnect` | transport: port, pakiet (LAG) lub połączenie logiczne (`service_key`, `partner_account`, `rides_on`) | `dx-c900-fra-lag`, `dx-c900-fra-1a/1b`, `er-c900-weu`, `ic-c900-fra-1` |
| `SecurityIPsecProfile` | parametry IKE/ESP tunelu, jeden profil na wiele tuneli | `IPSEC-AES256-SHA256-DH14` |
| `CloudVPNConnection` | transport bez fizyki: trzyma tunele; nie jest końcem obwodu | `vpn-c900-aws-euc1` |
| `CloudPeeringInterface` | zakończenie BGP w chmurze | `vif-c900-aws-transit`, `er-c900-weu-pri`, `gcp-c900-vlan-att-1`, `vpn-c900-aws-euc1-t1`, `webex-c900-fra` |
| `CloudGateway` | brama krawędziowa, VPN, customer gateway | `dxgw-c900`, `vgw-c900-euc1`, `cgw-c900-dc90`, `ergw-c900-weu`, `cr-c900-euw3` |
| `CloudTransitGateway`, `CloudVirtualNetwork` | bez zmian | `tgw-c900-euc1`, `vpc-c900-prod` |

Usunięte: `CloudVirtualInterface`, `TopologySaasConnection` (ich fakty są w `CloudPeeringInterface`) oraz
`TopologyProviderConnection` (decyzja właściciela: na razie ignorujemy żądanie usługi; nic nie wiąże nogi, bo wiążą
ją obwody i ich `endpoints`).

Końce obwodu to jedno pole `endpoints`: interfejs albo obiekt, który kończy obwód (transport, peering). Partnerem
pola jest cienki generyk `TopologyCircuitEnd`; pole i wspólna krawędź interfejsów (`ManagedGenericInterfaces.capabilities`)
nie zostały ruszone. Generyk nie ma HFID, więc koniec w pliku obiektów to zagnieżdżony obiekt z `kind` i `data`:
transport powstaje przy obwodzie nośnika, peering przy obwodzie sesji; peering nazywa potem swój transport, bramę,
konto i klucz przez HFID (`transport: dx-c900-fra-lag`, bo marker `CloudTransport` ma `name` i HFID). Transport ExpressRoute
i peering Webex kończą po dwa obwody, więc są wypisane dwa razy z tymi samymi wartościami.

## Pule (konwencja, bez pól)

Pól puli nie ma. Konwencja: nasz adres to /31 (para tunelowa /30) z `Handoff-<namespace>-IPv4` (opis `ip_namespace`
obwodu sesji), nasz prywatny AS z `PRIVATE-ASN32`; dostawca przydzielający zakres sam (Azure /30, GCP /29, AWS VPN
169.254/30) daje go jako `link_prefix` obwodu. Plik obiektów nie umie pobierać z puli, więc przydzielone wartości są
wpisane na sztywno: nasz adres w podinterfejsie (`06_dc_routing.yml`), nasz AS w `local_as` procesu BGP, adres i AS
dostawcy w `peer_ip` i `peer_asn` peeringu (`11_circuits.yml`).

## Pokrycie scenariuszy

Pełne = jest w modelu i w danych; częściowe = jest pole, ale uproszczone; brak = nie modelowane.

| Scenariusz | Pokrycie | Uwagi |
| ---------- | -------- | ----- |
| AWS DX dedykowane, transit VIF -> DX Gateway -> TGW -> VPC | pełne | `dx-c900-fra-lag`, `vif-c900-aws-transit` (scope `private`), `dxgw-c900` (asocjacja z TGW w `transit_gateway`) |
| AWS DX, private VIF -> VGW -> VPC | pełne | `vif-c900-aws-private`, `vgw-c900-euc1` |
| AWS DX hostowane (konto partnera), LAG | pełne | `partner_account` (GCP Partner Interconnect); LAG: pakiet `dx-c900-fra-lag` i członkowie `dx-c900-fra-1a/1b` (`rides_on`) |
| AWS public VIF | częściowe | `scope: public` i `advertised_prefixes` są w modelu, bez przykładu |
| Azure ExpressRoute: obwód, dwa łącza, dwa peeringi /30, ExpressRoute Gateway w VNet | pełne | jeden `er-c900-weu`, `er-c900-weu-pri/-sec`, `ergw-c900-weu` |
| Azure Microsoft peering | częściowe | `scope: public` + `advertised_prefixes`, bez przykładu; route filters nie modelowane |
| GCP Interconnect: VLAN attachment -> Cloud Router -> VPC | pełne | Partner Interconnect, `gcp-c900-vlan-att-1`, `cr-c900-euw3` |
| AWS S2S VPN: customer gateway, dwa tunele z BGP, strona TGW | pełne | `cgw-c900-dc90`, dwa tunele; strona VGW to `CloudVPNConnection.gateway` (bez przykładu) |
| SaaS: Webex peering BGP, Zscaler tunel bez BGP | pełne | `scope: saas`, `routing: bgp / static` |
| Parametry IKE/IPsec, klucz tunelu | pełne | `SecurityIPsecProfile` (9 pól), `tunnel_key`, `tunnel_mode`, `endpoint_ip` na tunelu |
| Route filters, allowed-prefix list DX | brak | route filters nie modelowane; allowed prefixes: runda 2 (`allowed_prefixes` załącznika) |
| Asocjacja DX Gateway z wieloma TGW / VGW | brak | `CloudGateway.transit_gateway` i `virtual_network` to jedna krawędź każda |
| Konto właściciela zasobu SaaS | brak | SaaS nie ma konta; jest `owner` i `service_id` |

## Ładowanie od zera

Dane ładuje się raz na czystym stosie (zagnieżdżony `segment_deployments` nie jest idempotentny przy ponownym
załadowaniu). Schemat usuwa rodzaje, czego serwer nie robi w miejscu, więc stos buduje się od nowa:

```bash
uv run invoke clean-testcontainers
INFRAHUB_TESTING_ENABLE_INTEGRATION=1 INFRAHUB_TESTING_KEEP_STACK=1 uv run invoke test-integration --tests tests/integration/test_01_setup.py
export INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=06438eb2-8019-4776-878c-0941b1f1d1ec
uv run infrahubctl object load data/demos/cloud_saas_model/
```

Nie ładować `data/demos/30_all/08_interconnects/05_provider_connections/`: używa usuniętych rodzajów.

## Szybki test modelu

`uv run invoke check-model` ładuje schemat i te dane na jednorazową gałąź, uruchamia `trace.py` jako bibliotekę
(`main_walk(..., quiet=True)` zwraca listy przeskoków L1 i L3) i sprawdza asercje oraz pliki wzorcowe, w kilka minut.
Opis: `tests/traversal/README.md`.

Wynik na stosie zbudowanym od zera (`--rebuild`, bez danych przykładowych na `main`, więc test ładuje pliki danych):

```text
  PASS  [cloud_saas_model] schema load
  PASS  [cloud_saas_model] data load data/demos/cloud_saas_model
  PASS  [c900-demo-web-p -> c900-demo-subnet-p] every object kind of the model non-empty
  PASS  [c900-demo-web-p -> c900-demo-subnet-p] no hop marked missing
  PASS  [c900-demo-web-p -> c900-demo-subnet-p] L1 reaches the cloud segment
  PASS  [c900-demo-web-p -> c900-demo-subnet-p] L1 legs per carrier type
  PASS  [c900-demo-web-p -> c900-demo-subnet-p] L3 reaches the cloud segment
  PASS  [c900-demo-web-p -> c900-demo-subnet-p] L3 legs per session circuit type
  PASS  [c900-demo-subnet-p -> c900-demo-web-p] reverse: peerings landing in vpc-c900-prod are the ones the DC reaches
  PASS  [c900-demo-web-p -> c900-demo-subnet-p] golden
  10 PASS, 0 FAIL   (kolejny przebieg: 16 s; z przebudową stosu 290 s)
```

## Przejście po grafie

```bash
INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=06438eb2-8019-4776-878c-0941b1f1d1ec \
  uv run python data/demos/cloud_saas_model/trace.py c900-demo-web-p c900-demo-subnet-p
```

`trace.py` czyta graf hurtowo (po jednym zapytaniu GraphQL na rodzaj) i idzie po nim w pamięci, od końców obwodów do
peeringu, bramy, transit gateway, sieci wirtualnej i segmentu chmurowego. Trzy sekcje: L1 (fizyka), L3 (routing) i
lista wszystkich peeringów z miejscem lądowania (także Azure, GCP, SaaS, które nie dochodzą do segmentu AWS).
Tunele VPN są łączone z nośnikiem internetowym przez `parent_interface` interfejsu `Tunnel<n>` (port `Ethernet1/2`).
Wynik z instancji 8100:

```text
=== L1 (physical): c900-demo-web-p -> c900-demo-subnet-p
  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/2]  (L2/L3 inside the device, no cable)
  --carrier INET-DC90-ISP-DEMO (internet): dc90-edge-01:Ethernet1/2 <-> EXT-ISP-DEMO-PE1:et-0/0/0
  [tunnels ['Tunnel1', 'Tunnel2', 'Tunnel3'] ride on dc90-edge-01:Ethernet1/2 through the carrier]
  --> private peering vpn-c900-aws-euc1-t1 --> transport VPN vpn-c900-aws-euc1 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod   [account c900-aws-prod]
  --> private peering vpn-c900-aws-euc1-t2 --> transport VPN vpn-c900-aws-euc1 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod   [account c900-aws-prod]
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/3]  (L2/L3 inside the device, no cable)
  --carrier XC-DC90-AWS-DX1 (cross_connect): dc90-edge-01:Ethernet1/3 <-> DirectConnect dx-c900-fra-1a
  --rides_on--> bundle dx-c900-fra-lag
  --> private peering vif-c900-aws-private --> cloud gateway vpn_gateway vgw-c900-euc1 --> virtual network vpc-c900-prod   [account c900-aws-prod]
  --> private peering vif-c900-aws-transit --> cloud gateway cloud_edge_gateway dxgw-c900 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod   [account c900-aws-prod]
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/8]  (L2/L3 inside the device, no cable)
  --carrier XC-DC90-AWS-DX2 (cross_connect): dc90-edge-01:Ethernet1/8 <-> DirectConnect dx-c900-fra-1b
  --rides_on--> bundle dx-c900-fra-lag
  --> private peering vif-c900-aws-private --> cloud gateway vpn_gateway vgw-c900-euc1 --> virtual network vpc-c900-prod   [account c900-aws-prod]
  --> private peering vif-c900-aws-transit --> cloud gateway cloud_edge_gateway dxgw-c900 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod   [account c900-aws-prod]
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

=== L3 (routed): c900-demo-web-p -> c900-demo-subnet-p
  segment c900-demo-web-p: gateway 10.90.1.1/24 prefix 10.90.1.0/24
  --> SVI dc90-leaf-01:Vlan100 (VLAN 100) in VRF dc90-vrf-prod [PROD]  (deployment DC90)
      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-DX (regular) via dc90-edge-01:Ethernet1/3.1000-->
      --session circuit SC-C900-AWS-DX-PROD (handoff): ends ['dc90-edge-01:Ethernet1/3.1000', 'PeeringInterface vif-c900-aws-transit private']
      --> private peering vif-c900-aws-transit (provider 10.255.128.1/31 AS64512) --> cloud gateway cloud_edge_gateway dxgw-c900 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> instance c900-app-euc1-1 10.200.1.11/24

      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-DX-PRIV (regular) via dc90-edge-01:Ethernet1/3.1100-->
      --session circuit SC-C900-AWS-DX-PRIV (handoff): ends ['dc90-edge-01:Ethernet1/3.1100', 'PeeringInterface vif-c900-aws-private private']
      --> private peering vif-c900-aws-private (provider 10.255.128.3/31 AS64512) --> cloud gateway vpn_gateway vgw-c900-euc1 --> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> instance c900-app-euc1-1 10.200.1.11/24

      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-VPN-1 (regular) via dc90-edge-01:Tunnel1-->
      --session circuit SC-C900-AWS-VPN-1 (vpn): ends ['dc90-edge-01:Tunnel1', 'PeeringInterface vpn-c900-aws-euc1-t1 private']
      --> private peering vpn-c900-aws-euc1-t1 (provider 169.254.10.1/30 AS64512) --> transport VPN vpn-c900-aws-euc1 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> instance c900-app-euc1-1 10.200.1.11/24

      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-VPN-2 (regular) via dc90-edge-01:Tunnel2-->
      --session circuit SC-C900-AWS-VPN-2 (vpn): ends ['dc90-edge-01:Tunnel2', 'PeeringInterface vpn-c900-aws-euc1-t2 private']
      --> private peering vpn-c900-aws-euc1-t2 (provider 169.254.10.5/30 AS64512) --> transport VPN vpn-c900-aws-euc1 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> instance c900-app-euc1-1 10.200.1.11/24

=== peering interfaces (the common cloud interface, every operator)
  er-c900-weu-pri [private, bgp] vlan 200 tunnel None mtu 1500 bfd True key None  (c900-azure-prod)
      provider 10.255.128.10/30 AS12076   advertised []
      rides on DirectConnect er-c900-weu logical service_key 11111111-2222-3333-4444-555555555555 partner None rides_on None
      lands: cloud gateway cloud_edge_gateway ergw-c900-weu --> virtual network vnet-c900-weu
  er-c900-weu-sec [private, bgp] vlan 200 tunnel None mtu 1500 bfd True key None  (c900-azure-prod)
      provider 10.255.128.14/30 AS12076   advertised []
      rides on DirectConnect er-c900-weu logical service_key 11111111-2222-3333-4444-555555555555 partner None rides_on None
      lands: cloud gateway cloud_edge_gateway ergw-c900-weu --> virtual network vnet-c900-weu
  gcp-c900-vlan-att-1 [private, bgp] vlan 400 tunnel None mtu 1460 bfd True key None  (c900-gcp-prod)
      provider 169.254.20.1/29 AS16550   advertised []
      rides on DirectConnect ic-c900-fra-1 logical service_key None partner partner-fabric-gcp rides_on None
      lands: cloud gateway cloud_edge_gateway cr-c900-euw3 --> virtual network vpc-c900-gcp
  vif-c900-aws-private [private, bgp] vlan 1100 tunnel None mtu 9001 bfd True key c900-aws-dx-bgp-key  (c900-aws-prod)
      provider 10.255.128.3/31 AS64512   advertised []
      rides on DirectConnect dx-c900-fra-lag physical service_key None partner None rides_on None
      lands: cloud gateway vpn_gateway vgw-c900-euc1 --> virtual network vpc-c900-prod
  vif-c900-aws-transit [private, bgp] vlan 1000 tunnel None mtu 8500 bfd True key c900-aws-dx-bgp-key  (c900-aws-prod)
      provider 10.255.128.1/31 AS64512   advertised []
      rides on DirectConnect dx-c900-fra-lag physical service_key None partner None rides_on None
      lands: cloud gateway cloud_edge_gateway dxgw-c900 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  vpn-c900-aws-euc1-t1 [private, bgp] vlan None tunnel 1 mtu 1436 bfd False key None  (c900-aws-prod)
      provider 169.254.10.1/30 AS64512 endpoint 203.0.113.50/32 ipsec profile IPSEC-AES256-SHA256-DH14 tunnel key c900-aws-vpn-t1-key   advertised []
      rides on VPNConnection vpn-c900-aws-euc1 customer gateway cgw-c900-dc90 (198.51.100.10/32 AS4200090001)
      lands: transport VPN vpn-c900-aws-euc1 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  vpn-c900-aws-euc1-t2 [private, bgp] vlan None tunnel 2 mtu 1436 bfd False key None  (c900-aws-prod)
      provider 169.254.10.5/30 AS64512 endpoint 203.0.113.51/32 ipsec profile IPSEC-AES256-SHA256-DH14 tunnel key c900-aws-vpn-t2-key   advertised []
      rides on VPNConnection vpn-c900-aws-euc1 customer gateway cgw-c900-dc90 (198.51.100.10/32 AS4200090001)
      lands: transport VPN vpn-c900-aws-euc1 --> transit gateway tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  webex-c900-fra [saas, bgp] vlan 300 tunnel None mtu None bfd False key c900-webex-bgp-key  (owner Cloud Demo GmbH)
      provider 10.255.128.7/31 AS13445   advertised []
      rides on (no transport object) region WEBEX-FRANKFURT
      lands: (no virtual network: SaaS)
  zscaler-c900-muc [saas, static] vlan None tunnel 1 mtu None bfd False key None  (owner Cloud Demo GmbH)
      provider None ASNone endpoint 203.0.113.80/32 gre profile None tunnel key None   advertised []
      rides on (no transport object) region ZSCALER-MUNICH I
      lands: (no virtual network: SaaS)
```

## Czego model nie wyraża (szczerze)

- L1: między portem `Ethernet1/13` krawędzi (koniec panelu) a portem nośnika (`Ethernet1/3`, `Ethernet1/2`) nie ma kabla:
  to przejście wewnątrz urządzenia, skrypt je tak oznacza.
- Kabel idzie od portu segmentu liścia do panelu krosowego, a nośnik zaczyna się na innym porcie krawędzi. Fizycznie
  spójna ścieżka wymagałaby dodatkowych urządzeń. Nie ma serwera w DC: pierwszy hop to port liścia z krawędzią `segments`.
- Strona dostawcy peeringu nie ma procesu BGP: peering BGP ma jeden proces (nasz), druga strona to `peer_ip` i `peer_asn`
  peeringu chmury, znajdowanego przez obwód sesji, który ma ten sam podinterfejs (krawędź `capabilities` peeringu BGP
  jest typu `DcimInterface`).
- Tunel jest powiązany z nośnikiem internetowym przez `parent_interface`, a nie przez obwód: obwód internetowy nie wie,
  jakie tunele po nim idą.
- Brak żądania usługi (`TopologyProviderConnection`): nic nie mówi "chcę połączyć A z B dla klienta X"; nogi składają się
  z obwodów i ich końców. Pule, `near_port` i redundancja primary/secondary nie mają pól.
- Wspólny rdzeń nie wyraża elementów specyficznych dla operatora: Direct Connect Gateway z wieloma asocjacjami,
  route filters Microsoft peering, ról Cloud Router
  (ASN, reklamowane zakresy), grup nadmiarowości Fabric. Powstaną w generatorze danego operatora (`connection_profile`).
- Segment VXLAN ma prefiks w `gateway.ip_prefix`, segment chmurowy w `cidr_block` (tekst): skrypt traktuje je różnie.
- Krawędź `segments` na porcie jest jednostronna (segment nie ma listy portów): skrypt czyta porty i filtruje po segmencie.
