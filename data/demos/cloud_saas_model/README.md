# Model połączeń z chmurą i SaaS: dane przykładowe

Gałąź `model/cloud-saas`, instancja `http://localhost:8100` (token `06438eb2-8019-4776-878c-0941b1f1d1ec`), gałąź Infrahub `main`.
Dane są napisane ręcznie: żaden generator, transform ani check nie działa. Ładują się na samych danych bootstrap
(nie zależą od `30_all`).

## Słownik wspólny dla wszystkich chmur

Operatorzy różnią się słownictwem, nie strukturą. Model ma wspólne elementy; słowo operatora (VIF, peering,
VLAN attachment, tunel) jest tylko w nazwach obiektów, opisach i w tabeli poniżej.

| Element wspólny | Rodzaj | Co to jest |
| --------------- | ------ | ---------- |
| transport | `CloudDirectConnect` (`physical` / `logical`, `rides_on`), `CloudVPNConnection` (generyk `CloudTransport` + `DcimDevice`) | urządzenie, które TRZYMA INTERFEJSY: porty (`DcimPhysicalInterface`), pakiet (`DcimLAGInterface`) i peeringi; `physical`: porty i pakiet w regionie dostawcy, `logical`: obwód na porcie partnera albo własnym, `CloudVPNConnection`: trzyma tunele; konto, `region` albo `saas_region`, `owner` (SaaS) są na transporcie |
| peering ("interfejs chmury") | `CloudPeeringInterface` (`DcimInterface` transportu) | zakończenie L3/BGP po stronie chmury: interfejs transportu jadący na jego porcie lub pakiecie (`parent_interface`); na nim kończy się obwód sesji (`capabilities`); adres i AS dostawcy to `peer_ip`, `peer_asn` |
| brama | `CloudGateway`, `gateway_type`: `hub` / `edge` / `customer` | `hub`: węzeł routingu z załącznikami i tabelami; `edge`: to, na czym peering lub VPN ląduje przed siecią; `customer`: nasza strona VPN (publiczny adres i AS), wymagana na połączeniu VPN |
| załącznik bramy | `CloudGatewayAttachment` | jedno połączenie bramy (`gateway`) z DOKŁADNIE jednym celem: `virtual_network` albo `peer_gateway` (check R1); `allowed_prefixes`: co ogłasza w stronę on-prem |
| tabela routingu | `CloudGatewayRouteTable` | domena routingu huba; załącznik ma `associated_route_table` i `propagates_to_route_tables` |
| sieć wirtualna | `CloudVirtualNetwork` | VPC / VNet; `peers` (symetryczne) to peering sieci |
| punkt usługi | `CloudServiceEndpoint` | prywatna ścieżka sieci do usługi (`access`: `address` albo `route`) |

Nasza strona to zawsze podinterfejs urządzenia i nasz proces BGP; nie ma jej w obiektach chmury.

`CloudPeeringInterface` (HFID: `[transport, interfejs]`, np. `[dx-c900-fra-lag, lag1.1000]`): jest `DcimInterface`, więc
ma `device` (transport), `name`, `mtu` (domyślnie 9000) i `capabilities` jak każdy port. Własne pola: `scope` (private,
public, saas), `routing` (bgp, static), `vlan_id` albo `tunnel_index`, `bfd`, `service_id`; tunel: `endpoint_ip`,
`tunnel_mode` (ipsec, gre), `crypto_profile` (`SecurityIPsecProfile`), `tunnel_key` (`RoutingPassword`, klucz IPsec; BGP
to `auth`); relacje `parent_interface` (port lub pakiet TEGO SAMEGO transportu; marker `DcimSubInterface`, ta sama krawędź
`sub__interface` co podinterfejs urządzenia: port i pakiet widzą peering w `sub_interfaces`, a peering ma `ip_address`), `gateway` (puste dla tunelu VPN: ląduje przez
`transport.gateway`), `auth` (`RoutingPassword`), `advertised_prefixes`, oraz `peer_ip`, `peer_asn` z
`TopologyConnectorRouting`. Konto, region i `owner` są transportu, nie peeringu.

Prefiksy mają po jednej liście na kierunek: `peering.advertised_prefixes` (MY -> chmura; tylko publiczne,
weryfikowane przez dostawcę, i statyczne; dla `private` puste) i `attachment.allowed_prefixes` (chmura -> MY; puste =
bloki CIDR sieci). `Gateway.announced_prefixes` nie istnieje: dozwolone prefiksy są per skojarzenie, nie per bramka.

## Tabela wzorców (blueprint) per operator

`OrganizationProvider.connection_profile` (aws, azure, gcp, oci, ibm, alibaba, ...) mówi, jaki jest wzorzec. Komórka:
wspólny element, w nawiasie słowo operatora.

| Słowo operatora | Wspólny element |
| --------------- | --------------- |
| AWS dedicated connection / ExpressRoute Direct port / Dedicated Interconnect / OCI cross-connect / IBM Dedicated / Alibaba physical connection | `CloudDirectConnect` `physical` |
| AWS LAG / ExpressRoute Direct para / OCI cross-connect group / Interconnect bundle | JEDEN `CloudDirectConnect` `physical` z portami członków (`DcimPhysicalInterface`) i pakietem (`DcimLAGInterface`, `member_interfaces`); peering jedzie na pakiecie |
| AWS hosted connection / ExpressRoute circuit (provider) / Partner Interconnect attachment / OCI VC (partner) / IBM Connect / Alibaba hosted | `CloudDirectConnect` `logical` + `partner_account`, `service_key` |
| ExpressRoute circuit na Direct, OCI VC na własnym CCG, Dedicated Interconnect attachment | `CloudDirectConnect` `logical` + `rides_on` transportu, który trzyma port lub pakiet; własna para portów (`msee-pri`, `msee-sec`) dla nośników |
| Dedicated Interconnect / OCI FastConnect: port routowany bez VLAN | `CloudDirectConnect` `physical` z portem; peering BEZ `vlan_id`, `parent_interface` = port; nasz koniec to FIZYCZNY port z adresem |
| Equinix Fabric / Megaport VXC | nośnik `virtual_connection`; koniec w chmurze = transport `logical` + `partner_account`; MCR / Network Edge = `CloudNetworkAppliance` |
| AWS VIF private / transit / public, ExpressRoute private / Microsoft peering, GCP VLAN attachment, OCI virtual circuit, IBM, Alibaba VBR | `CloudPeeringInterface` `scope` private lub public (+ `vlan_id`, `peer_ip`, `peer_asn`, `auth`) |
| Public VIF / Microsoft peering / OCI public VC: nasze publiczne prefiksy | `peering.advertised_prefixes` (MY -> chmura) |
| Webex, M365 przez ER, Salesforce przez Fabric, Zscaler | transport z `saas_region` i `owner`, bez konta (`CloudDirectConnect` z portem albo `CloudVPNConnection` bez `customer_gateway`) + `CloudPeeringInterface` `scope: saas` (`service_id`) |
| AWS S2S tunnel / Azure VPN / GCP HA VPN tunnel / OCI IPSec tunnel | `CloudPeeringInterface` (`tunnel_index`, `endpoint_ip`, `tunnel_mode`, `crypto_profile`, `tunnel_key`) |
| AWS VPN connection / Azure connection / OCI IPSec connection / IBM VPN connection | `CloudVPNConnection` (`customer_gateway`, `gateway`) |
| AWS customer gateway / Azure local network gateway / GCP peer VPN gateway / OCI CPE | `CloudGateway` `customer` (`ip_address`, `asn`) |
| Zscaler / Netskope / Cloudflare GRE | peering `saas`, `routing: static`, `tunnel_mode: gre`, `endpoint_ip` |
| AWS Transit Gateway / Azure vWAN hub / GCP NCC hub / OCI DRG / IBM Transit Gateway / Alibaba CEN Transit Router | `CloudGateway` `hub` (globalny: bez `region`) |
| AWS Direct Connect Gateway / virtual private gateway / Azure ER gateway, VPN gateway / GCP Cloud Router / Alibaba VBR | `CloudGateway` `edge` |
| TGW route table + association (1) + propagation (N) / vWAN hub route table / DRG route table | `CloudGatewayRouteTable` + `associated_route_table`, `propagates_to_route_tables` |
| TGW VPC attachment / hub VNet connection / NCC VPC spoke / DRG VCN attachment / VGW lub Cloud Router w sieci | `CloudGatewayAttachment` `gateway` + `virtual_network` |
| DXGW skojarzenie z TGW lub VGW (allowed prefixes) / ER gateway w hubie / TGW peering / DRG RPC / hub-hub | `CloudGatewayAttachment` `gateway` + `peer_gateway` (+ `allowed_prefixes`) |
| TGW VPN attachment, NCC hybrid spoke, DRG IPSec attachment | `CloudVPNConnection.gateway` = hub (bez załącznika) |
| TGW Connect (GRE), GCP router appliance, vWAN NVA, IBM GRE | peering `private`, `gateway` = hub, `tunnel_mode: gre` |
| VPC peering / VNet peering / local peering | `CloudVirtualNetwork.peers` |
| AWS interface endpoint / Azure private endpoint / GCP PSC | `CloudServiceEndpoint` `access: address` |
| AWS gateway endpoint / OCI service gateway / Azure service endpoint / GCP Private Google Access | `CloudServiceEndpoint` `access: route` |
| AWS / GCP / Azure / OCI / IBM subnet | `CloudNetworkSegment` `cloud_subnet` |

## Mapa obiektów

| Rodzaj | Rola | Przykład |
| ------ | ---- | -------- |
| `TopologyCarrierCircuit` | fizyka: przewód, cross-connect, wirtualne połączenie, internet; końce to `capabilities` (port nasz i port transportu) | `XC-DC90-AWS-DX1`, `INET-DC90-ISP-DEMO` |
| `TopologySessionCircuit` | wirtualne: handoff / vpn / tunnel; końce to `capabilities` (nasz podinterfejs lub port routowany i peering transportu) | `SC-C900-AWS-DX-PROD`, `SC-C900-GCP-DED` |
| `CloudDirectConnect` | transport-urządzenie: porty, pakiet (LAG) i peeringi; logiczne: `service_key`, `partner_account`, `rides_on` | `dx-c900-fra-lag`, `er-c900-weu`, `ic-c900-fra-1`, `ic-ded-c900-fra`, `webex-c900-fra-xc` |
| `DcimPhysicalInterface`, `DcimLAGInterface` | porty i pakiet transportu (`device` = transport) | `dxcon-1a`, `dxcon-1b`, `lag1`, `msee-pri`, `ic-dedicated-port` |
| `SecurityIPsecProfile` | parametry IKE/ESP tunelu, jeden profil na wiele tuneli | `IPSEC-AES256-SHA256-DH14` |
| `CloudVPNConnection` | transport bez portów: trzyma tunele (interfejsy); SaaS: `saas_region` i `owner` | `vpn-c900-aws-euc1`, `zscaler-c900-muc` |
| `CloudPeeringInterface` | zakończenie BGP w chmurze, interfejs transportu | `lag1.1000`, `er-c900-weu-pri`, `gcp-c900-vlan-att-1`, `gcp-c900-ded-1`, `Tunnel1`, `webex-c900-fra` |
| `CloudGateway` | hub, brama krawędziowa, customer gateway | `tgw-c900-euc1` (hub), `dxgw-c900`, `vgw-c900-euc1`, `ergw-c900-weu`, `cr-c900-euw3` (edge), `cgw-c900-dc90` (customer) |
| `CloudGatewayAttachment`, `CloudGatewayRouteTable` | połączenia bram i domena routingu huba | `tgwa-c900-vpc`, `tgwa-c900-dxgw`, `rt-c900-prod` |
| `CloudVirtualNetwork` | sieć wirtualna; `peers` | `vpc-c900-prod`, `vnet-c900-weu` + `vnet-c900-weu-spoke` |
| `CloudServiceEndpoint` | prywatna ścieżka do usługi | `vpce-c900-s3`, `vpce-c900-webex` |

Usunięte: `CloudVirtualInterface`, `TopologySaasConnection` (ich fakty są w `CloudPeeringInterface`),
`TopologyProviderConnection` (decyzja właściciela: na razie ignorujemy żądanie usługi; nic nie wiąże nogi, bo wiążą
ją obwody i ich `capabilities`) oraz `CloudTransitGateway` (jest `CloudGateway` typu `hub`).

Obwód kończy się na interfejsach przez JEDNĄ krawędź, `capabilities` (wspólna krawędź `ManagedGenericInterfaces.capabilities`,
partner `DcimInterface`, min 1, max 2 na każdym obwodzie). Interfejs ma tylko `capabilities` jako krawędź obwodu; nie ma
pola `endpoints`, generyka `TopologyCircuitEnd` ani `far_end`. Koniec w pliku obiektów to HFID interfejsu
`[urządzenie, interfejs]`: `["dc90-edge-01", "Ethernet1/3"]`, `["dx-c900-fra-lag", "dxcon-1a"]`. Dlatego obiekty muszą
istnieć wcześniej, a pliki ładują się alfabetycznie i po kolei: `08_cloud.yml` (konta, bramy) -> `09_saas.yml` -> `10b`
transporty -> `10c` porty -> `10d` pakiet -> `10e` peeringi -> `11` obwody -> `12` ręczne sesje BGP. Zależne obiekty są w
późniejszym pliku, bo indeks HFID obiektów z tego samego przebiegu bywa opóźniony.

Sesje BGP nóg chmurowych są pisane ręcznie (`12_peerings.yml`: nasz podinterfejs albo port routowany w `capabilities`, nasz
proces); żaden generator ich nie buduje, a `add_virtual_circuit` nie ma budować sesji dla obwodu, którego koniec to peering
chmury.

Check `checks/cloud_connection.py` (`validate_cloud_connection`, zapytanie `cloud_connection`) trzyma to, czego schemat
nie powie (serwer przyjmuje załącznik bez celu, z dwoma celami, pętlę `rides_on` i peering jadący na porcie innego
urządzenia): R1 załącznik ma dokładnie jeden cel, R2 tabela routingu należy do huba i do bramy załącznika, R3 `rides_on`
bez pętli, fizyczny tylko na fizycznym, logiczny na naszym transporcie bez `partner_account`, R4 segmenty punktu usługi są z
jego sieci i klucz tunelu wymaga IPsec, R5 profil krypto tylko przy IPsec, R6 tunel VPN ma `endpoint_ip` i jest
interfejsem `CloudVPNConnection`, R7 wycofana (członków pakietu pilnuje serwer, `parent_interface` peeringu
`checks/interface_parents.py`), R8 peering z `vlan_id` jedzie na porcie lub pakiecie, rodzicem peeringu jest port lub pakiet,
VLAN-y jednego rodzica są różne (ten sam VLAN na dwóch portach jest dozwolony: ExpressRoute), R9 miejsce transportu:
dokładnie jedno z `region` i `saas_region`, z kontem bez `owner`, bez konta z `saas_region` i `owner`; peering jest
interfejsem transportu, a `scope: saas` leży na transporcie z `saas_region`.

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
| AWS DX dedykowane, transit VIF -> DX Gateway -> TGW -> VPC | pełne | `dx-c900-fra-lag`, `lag1.1000` (scope `private`), `dxgw-c900` (edge) -> `tgwa-c900-dxgw` (`allowed_prefixes 10.200.0.0/16`) -> `tgw-c900-euc1` (hub) -> `tgwa-c900-vpc` |
| AWS DX, private VIF -> VGW -> VPC | pełne | `lag1.1100`, `vgw-c900-euc1` |
| AWS DX hostowane (konto partnera), LAG | pełne | `partner_account` (GCP Partner Interconnect); LAG: jeden transport `dx-c900-fra-lag` z portami `dxcon-1a/1b` i pakietem `lag1`, dwa nośniki `XC-DC90-AWS-DX1/2` |
| GCP Dedicated Interconnect: port routowany bez VLAN | pełne | `ic-ded-c900-fra`, port `ic-dedicated-port`, peering `gcp-c900-ded-1` bez `vlan_id`; nasz koniec to fizyczny `Ethernet1/9` z adresem, obwód `SC-C900-GCP-DED` |
| AWS public VIF | częściowe | `scope: public` i `advertised_prefixes` (MY -> chmura) są w modelu, bez przykładu |
| Azure ExpressRoute: obwód, dwa łącza, dwa peeringi /30, ExpressRoute Gateway w VNet | pełne | jeden `er-c900-weu` z portami `msee-pri/-sec` (końce nośników), `er-c900-weu-pri/-sec`, `ergw-c900-weu` + `ergwa-c900-weu`; spoke przez `peers` |
| Azure Microsoft peering | częściowe | `scope: public` + `advertised_prefixes`, bez przykładu; route filters nie modelowane |
| GCP Interconnect: VLAN attachment -> Cloud Router -> VPC | pełne | Partner Interconnect, `gcp-c900-vlan-att-1`, `cr-c900-euw3` |
| AWS S2S VPN: customer gateway, dwa tunele z BGP, strona TGW | pełne | `cgw-c900-dc90` (`customer`), dwa tunele; strona dostawcy to `CloudVPNConnection.gateway` (tu hub; VGW bez przykładu) |
| SaaS: Webex peering BGP, Zscaler tunel bez BGP | pełne | transporty z `saas_region` i `owner` bez konta (`webex-c900-fra-xc`, `zscaler-c900-muc`), `scope: saas`, `routing: bgp / static` |
| Parametry IKE/IPsec, klucz tunelu | pełne | `SecurityIPsecProfile` (9 pól), `tunnel_key`, `tunnel_mode`, `endpoint_ip` na tunelu |
| Allowed-prefix list DX | pełne | `allowed_prefixes` załącznika `tgwa-c900-dxgw` |
| Route filters | brak | nie modelowane |
| VNet peering, punkt usługi (S3, PrivateLink) | pełne | `vnet-c900-weu-spoke` `peers` `vnet-c900-weu`; `vpce-c900-s3` (route), `vpce-c900-webex` (address) |
| Asocjacja DX Gateway z wieloma TGW / VGW | pełne | kilka załączników z `peer_gateway` = ta sama brama |
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

## Szybki test modelu

`uv run invoke check-model` ładuje schemat i te dane na jednorazową gałąź, uruchamia `trace.py` jako bibliotekę
(`main_walk(..., quiet=True)` zwraca listy przeskoków L1 i L3) i sprawdza asercje oraz pliki wzorcowe, w kilka minut.
Opis: `tests/traversal/README.md`.

Asercje na parę segmentów: wszystkie rodzaje obiektów niepuste (także `lag`), żaden przeskok nie jest oznaczony jako
brakujący (m.in. każdy obwód jest wymieniony w `capabilities` interfejsów, na których się kończy), L1 i L3 docierają do
segmentu chmurowego, liczba nóg wg typu obwodu, każdy obwód nośnika i sesji kończy się na interfejsie transportu
(`CloudDirectConnect` albo `CloudVPNConnection`), transport `dx-c900-fra-lag` trzyma port, pakiet i VIF, kierunek
odwrotny i plik wzorcowy.

## Przejście po grafie

| Warstwa | Krok | Krawędź |
| ------- | ---- | ------- |
| L1 | nasz port krawędzi -> obwód nośnika | `DcimInterface.capabilities` |
| L1 | obwód -> port po drugiej stronie | `TopologyCarrierCircuit.capabilities` |
| L1 | port -> transport | `device` portu (`CloudDirectConnect`) |
| L1 | port -> pakiet | `lag` portu (`lag__members`) |
| L1 | port lub pakiet -> VIF | `sub_interfaces` portu lub pakietu albo `CloudPeeringInterface(parent_interface__ids: [...])` |
| L1 | VIF -> brama -> załączniki -> sieć wirtualna -> segment chmurowy | `gateway`, `CloudGatewayAttachment`, `virtual_network`, `network_segments` |
| L1 | tunel: nasz `Tunnel<n>` jedzie na porcie nośnika internetowego | `DcimVirtualInterface.parent_interface` |
| L3 | podinterfejs lub port routowany -> obwód sesji | `DcimInterface.capabilities` |
| L3 | obwód sesji -> peering po drugiej stronie | `TopologySessionCircuit.capabilities` |
| L3 | peering -> brama (tunel: brama transportu `CloudVPNConnection.gateway`) -> sieć wirtualna | `gateway`, `device.gateway` |

```bash
INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=06438eb2-8019-4776-878c-0941b1f1d1ec \
  uv run python data/demos/cloud_saas_model/trace.py c900-demo-web-p c900-demo-subnet-p
```

`trace.py` czyta graf hurtowo (po jednym zapytaniu GraphQL na rodzaj) i idzie po nim w pamięci, od końców obwodów do
peeringu, bramy, załączników (i `allowed_prefixes`), huba, sieci wirtualnej (z `peers`) i segmentu chmurowego, z punktem
usługi segmentu. Trzy sekcje: L1 (fizyka), L3 (routing) i
lista wszystkich peeringów z miejscem lądowania (także Azure, GCP, SaaS, które nie dochodzą do segmentu AWS).
Obwód jest znajdowany od NASZEGO interfejsu przez jego `capabilities`; jego drugi koniec to interfejs transportu
(`device` = `CloudDirectConnect` albo `CloudVPNConnection`). Tunele VPN są łączone z nośnikiem internetowym przez
`parent_interface` interfejsu `Tunnel<n>` (port `Ethernet1/2`).
Wynik z instancji 8100:

```text
=== L1 (physical): c900-demo-web-p -> c900-demo-subnet-p
  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/2]  (L2/L3 inside the device, no cable)
  --carrier INET-DC90-ISP-DEMO (internet): EXT-ISP-DEMO-PE1:et-0/0/0 <-> dc90-edge-01:Ethernet1/2
  [tunnels ['Tunnel1', 'Tunnel2', 'Tunnel3'] ride on dc90-edge-01:Ethernet1/2 through the carrier]
  --> private peering Tunnel1 --> transport VPN vpn-c900-aws-euc1 --> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  --> private peering Tunnel2 --> transport VPN vpn-c900-aws-euc1 --> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/3]  (L2/L3 inside the device, no cable)
  --carrier XC-DC90-AWS-DX1 (cross_connect): dc90-edge-01:Ethernet1/3 <-> dx-c900-fra-lag:dxcon-1a
  --> transport dx-c900-fra-lag port dxcon-1a in lag1
  --> private peering lag1.1000 --> cloud gateway edge dxgw-c900 --attachment tgwa-c900-dxgw (allowed 10.200.0.0/16)--> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  --> private peering lag1.1100 --> cloud gateway edge vgw-c900-euc1 --attachment vgwa-c900-vpc--> virtual network vpc-c900-prod
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

  segment c900-demo-web-p --segments edge--> dc90-leaf-01:Ethernet1/1
  --cable CBL-DC90-LEAF01-PP01-F1--> panel dmo-1-s-1-r-1-1:PP-DC90-01:F1 (front)
  == jack pairing (front.rear_port) ==> panel dmo-1-s-1-r-1-1:PP-DC90-01:1 (rear)
  --cable CBL-DC90-PP01-R1-EDGE01--> dc90-edge-01:Ethernet1/13
  [device dc90-edge-01: dc90-edge-01:Ethernet1/13 ... dc90-edge-01:Ethernet1/8]  (L2/L3 inside the device, no cable)
  --carrier XC-DC90-AWS-DX2 (cross_connect): dc90-edge-01:Ethernet1/8 <-> dx-c900-fra-lag:dxcon-1b
  --> transport dx-c900-fra-lag port dxcon-1b in lag1
  --> private peering lag1.1000 --> cloud gateway edge dxgw-c900 --attachment tgwa-c900-dxgw (allowed 10.200.0.0/16)--> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  --> private peering lag1.1100 --> cloud gateway edge vgw-c900-euc1 --attachment vgwa-c900-vpc--> virtual network vpc-c900-prod
  --> cloud segment c900-demo-subnet-p (10.200.1.0/24)

=== L3 (routed): c900-demo-web-p -> c900-demo-subnet-p
  segment c900-demo-web-p: gateway 10.90.1.1/24 prefix 10.90.1.0/24
  --> SVI dc90-leaf-01:Vlan100 (VLAN 100) in VRF dc90-vrf-prod [PROD]  (deployment DC90)
      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-DX (regular) via dc90-edge-01:Ethernet1/3.1000-->
      --session circuit SC-C900-AWS-DX-PROD (handoff): ends ['dc90-edge-01:Ethernet1/3.1000', 'dx-c900-fra-lag:lag1.1000']
      --> private peering lag1.1000 (provider 10.255.128.1/31 AS64512) --> cloud gateway edge dxgw-c900 --attachment tgwa-c900-dxgw (allowed 10.200.0.0/16)--> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> service endpoint vpce-c900-s3 (route, provider P004)
      --> instance c900-app-euc1-1 10.200.1.11/24

      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-DX-PRIV (regular) via dc90-edge-01:Ethernet1/3.1100-->
      --session circuit SC-C900-AWS-DX-PRIV (handoff): ends ['dc90-edge-01:Ethernet1/3.1100', 'dx-c900-fra-lag:lag1.1100']
      --> private peering lag1.1100 (provider 10.255.128.3/31 AS64512) --> cloud gateway edge vgw-c900-euc1 --attachment vgwa-c900-vpc--> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> service endpoint vpce-c900-s3 (route, provider P004)
      --> instance c900-app-euc1-1 10.200.1.11/24

      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-VPN-1 (regular) via dc90-edge-01:Tunnel1-->
      --session circuit SC-C900-AWS-VPN-1 (vpn): ends ['dc90-edge-01:Tunnel1', 'vpn-c900-aws-euc1:Tunnel1']
      --> private peering Tunnel1 (provider 169.254.10.1/30 AS64512) --> transport VPN vpn-c900-aws-euc1 --> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> service endpoint vpce-c900-s3 (route, provider P004)
      --> instance c900-app-euc1-1 10.200.1.11/24

      BGP process dc90-leaf-01-bgp (AS4200090002)
      --peering BGP-DC90-LEAF01-EDGE01 (overlay)--> BGP process dc90-edge-01-bgp (AS4200090001)
      --handoff peering BGP-DC90-EDGE01-AWS-VPN-2 (regular) via dc90-edge-01:Tunnel2-->
      --session circuit SC-C900-AWS-VPN-2 (vpn): ends ['dc90-edge-01:Tunnel2', 'vpn-c900-aws-euc1:Tunnel2']
      --> private peering Tunnel2 (provider 169.254.10.5/30 AS64512) --> transport VPN vpn-c900-aws-euc1 --> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
      --> prefix ['10.200.0.0/16'] --> cloud segment c900-demo-subnet-p 10.200.1.0/24
      --> service endpoint vpce-c900-s3 (route, provider P004)
      --> instance c900-app-euc1-1 10.200.1.11/24

=== peering interfaces (the common cloud interface, every operator)
  Tunnel1 [private, bgp] vlan None tunnel 1 parent None mtu 1436 bfd False key None  (c900-aws-prod)
      provider 169.254.10.1/30 AS64512 endpoint 203.0.113.50/32 ipsec profile IPSEC-AES256-SHA256-DH14 tunnel key c900-aws-vpn-t1-key   advertised []
      interface of VPNConnection vpn-c900-aws-euc1 customer gateway cgw-c900-dc90 (198.51.100.10/32 AS4200090001)
      lands: transport VPN vpn-c900-aws-euc1 --> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  Tunnel1 [saas, static] vlan None tunnel 1 parent None mtu 9000 bfd False key None  (owner Cloud Demo GmbH)
      provider None ASNone endpoint 203.0.113.80/32 gre profile None tunnel key None   advertised []
      interface of VPNConnection zscaler-c900-muc (SaaS region ZSCALER-MUNICH I)
      lands: (no virtual network: SaaS)
  Tunnel2 [private, bgp] vlan None tunnel 2 parent None mtu 1436 bfd False key None  (c900-aws-prod)
      provider 169.254.10.5/30 AS64512 endpoint 203.0.113.51/32 ipsec profile IPSEC-AES256-SHA256-DH14 tunnel key c900-aws-vpn-t2-key   advertised []
      interface of VPNConnection vpn-c900-aws-euc1 customer gateway cgw-c900-dc90 (198.51.100.10/32 AS4200090001)
      lands: transport VPN vpn-c900-aws-euc1 --> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  er-c900-weu-pri [private, bgp] vlan 200 tunnel None parent msee-pri mtu 1500 bfd True key None  (c900-azure-prod)
      provider 10.255.128.10/30 AS12076   advertised []
      interface of DirectConnect er-c900-weu logical service_key 11111111-2222-3333-4444-555555555555 partner None rides_on None
      lands: cloud gateway edge ergw-c900-weu --attachment ergwa-c900-weu--> virtual network vnet-c900-weu (peered with vnet-c900-weu-spoke)
  er-c900-weu-sec [private, bgp] vlan 200 tunnel None parent msee-sec mtu 1500 bfd True key None  (c900-azure-prod)
      provider 10.255.128.14/30 AS12076   advertised []
      interface of DirectConnect er-c900-weu logical service_key 11111111-2222-3333-4444-555555555555 partner None rides_on None
      lands: cloud gateway edge ergw-c900-weu --attachment ergwa-c900-weu--> virtual network vnet-c900-weu (peered with vnet-c900-weu-spoke)
  gcp-c900-ded-1 [private, bgp] vlan None tunnel None parent ic-dedicated-port mtu 1500 bfd True key None  (c900-gcp-prod)
      provider 169.254.30.1/29 AS16550   advertised []
      interface of DirectConnect ic-ded-c900-fra physical service_key None partner None rides_on None
      lands: cloud gateway edge cr-c900-euw3 --attachment cra-c900-euw3--> virtual network vpc-c900-gcp
  gcp-c900-vlan-att-1 [private, bgp] vlan 400 tunnel None parent ic-port-1 mtu 1460 bfd True key None  (c900-gcp-prod)
      provider 169.254.20.1/29 AS16550   advertised []
      interface of DirectConnect ic-c900-fra-1 logical service_key None partner partner-fabric-gcp rides_on None
      lands: cloud gateway edge cr-c900-euw3 --attachment cra-c900-euw3--> virtual network vpc-c900-gcp
  lag1.1000 [private, bgp] vlan 1000 tunnel None parent lag1 mtu 8500 bfd True key c900-aws-dx-bgp-key  (c900-aws-prod)
      provider 10.255.128.1/31 AS64512   advertised []
      interface of DirectConnect dx-c900-fra-lag physical service_key None partner None rides_on None
      lands: cloud gateway edge dxgw-c900 --attachment tgwa-c900-dxgw (allowed 10.200.0.0/16)--> cloud gateway hub tgw-c900-euc1 --attachment tgwa-c900-vpc--> virtual network vpc-c900-prod
  lag1.1100 [private, bgp] vlan 1100 tunnel None parent lag1 mtu 9001 bfd True key c900-aws-dx-bgp-key  (c900-aws-prod)
      provider 10.255.128.3/31 AS64512   advertised []
      interface of DirectConnect dx-c900-fra-lag physical service_key None partner None rides_on None
      lands: cloud gateway edge vgw-c900-euc1 --attachment vgwa-c900-vpc--> virtual network vpc-c900-prod
  webex-c900-fra [saas, bgp] vlan 300 tunnel None parent wbx-port-1 mtu 9000 bfd False key c900-webex-bgp-key  (owner Cloud Demo GmbH)
      provider 10.255.128.7/31 AS13445   advertised []
      interface of DirectConnect webex-c900-fra-xc physical service_key None partner None rides_on None (SaaS region WEBEX-FRANKFURT)
      lands: (no virtual network: SaaS)
```

## Czego model nie wyraża (szczerze)

- L1: między portem `Ethernet1/13` krawędzi (koniec panelu) a portem nośnika (`Ethernet1/3`, `Ethernet1/2`) nie ma kabla:
  to przejście wewnątrz urządzenia, skrypt je tak oznacza.
- Kabel idzie od portu segmentu liścia do panelu krosowego, a nośnik zaczyna się na innym porcie krawędzi. Fizycznie
  spójna ścieżka wymagałaby dodatkowych urządzeń. Nie ma serwera w DC: pierwszy hop to port liścia z krawędzią `segments`.
- Strona dostawcy peeringu nie ma procesu BGP: peering BGP ma jeden proces (nasz), druga strona to `peer_ip` i `peer_asn`
  peeringu chmury, znajdowanego przez obwód sesji, który ma ten sam interfejs (podinterfejs albo port routowany) w
  `capabilities`.
- Tunel jest powiązany z nośnikiem internetowym przez `parent_interface`, a nie przez obwód: obwód internetowy nie wie,
  jakie tunele po nim idą.
- Brak żądania usługi (`TopologyProviderConnection`): nic nie mówi "chcę połączyć A z B dla klienta X"; nogi składają się
  z obwodów i ich końców. Pule, `near_port` i redundancja primary/secondary nie mają pól.
- Wspólny rdzeń nie wyraża elementów specyficznych dla operatora: route filters Microsoft peering, SKU ExpressRoute,
  FastPath, GatewaySubnet, import-distribution OCI, Global Reach, listy propozycji IKE, NAT-T, certyfikaty, DNS, limity
  prefiksów, timery BFD, wpisy tras. Powstaną w generatorze danego operatora (`connection_profile`) ze stałych w `utils/limits.py`.
- Pętla `rides_on` na samego siebie: serwer ją przyjmuje, ale odczyt zwraca `rides_on` puste (i kasuje poprzednią krawędź),
  więc check R3 widzi pętle co najmniej dwóch obiektów.
- `parent_interface` peeringu (marker `DcimSubInterface`, ta sama krawędź co podinterfejs urządzenia) nie ma `common_parent`,
  więc serwer nie pilnuje, że rodzic jest na tym samym transporcie (pakiet i jego członkowie: serwer pilnuje): to
  `checks/interface_parents.py` (reguła 1). Pierwszy peering na porcie transportu partnera przejdzie schemat.
- Sesje BGP nóg chmurowych są ręczne; nic nie alokuje /31 ani AS dla obwodu, którego koniec to peering chmury.
- Segment VXLAN ma prefiks w `gateway.ip_prefix`, segment chmurowy w `cidr_block` (tekst): skrypt traktuje je różnie.
- Krawędź `segments` na porcie jest jednostronna (segment nie ma listy portów): skrypt czyta porty i filtruje po segmencie.
