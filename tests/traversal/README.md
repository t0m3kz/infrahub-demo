# Szybki test modelu (traversal)

Zamiast godziny testów integracyjnych: schemat + mały model na jednorazowej gałęzi, potem asercje na przejściu po
grafie (L1: kable, porty, patch panele, obwody nośników, transport z portami i pakietem; L3: SVI/VRF, peeringi BGP, obwody sesji,
interfejs peeringu chmury, brama, hub, sieć wirtualna, segment chmurowy) w obu kierunkach. Cel: kilka minut.

```bash
uv run invoke check-model                      # wszystkie przypadki z cases.yml
uv run invoke check-model --case full_topology
uv run invoke check-model --update-golden      # zapis plików wzorcowych tests/traversal/golden/*.txt
uv run invoke check-model --keep-branch        # zostaw gałąź tv-* do oględzin
uv run invoke check-model --rebuild            # wyczyść kontenery testowe i zbuduj stos od zera
uv run invoke check-model --force-load         # ładuj dane mimo że już są na main (zwykle się nie uda)
```

Kroki: (1) stos na `:8100` (jeśli nie odpowiada na `/api/info`, runner startuje go jak test integracyjny `test_01`;
jednorazowo ok. 5 minut, kontenery zostają); (2) gałąź `tv-<czas>`; (3) `infrahubctl schema load` z drzewa roboczego
na tej gałęzi (odmowy serwera są wypisane dosłownie); (4) `infrahubctl object load` katalogów danych, po kolei;
(5) dla każdej pary segmentów: odczyt grafu, przejście L1 i L3, asercje, porównanie z plikiem wzorcowym; (6) usunięcie
gałęzi (także po błędzie) i czas całkowity. Nic nie jest zapisywane na `main`.

Ścieżki są serwera (`utils/path_trace.py`, `traverse_paths`): para w `cases.yml` podaje końce jako `rodzaj:nazwa`
(`src`, `dst`) i, gdy cel ma być osiągalny przez konkretny transport, `through` (jedno śledzenie `--via` na transport).
Asercje na parę: L1 i L3 docierają do celu, zbiór ścieżek równego kosztu (ECMP, minimalna głębokość) jest kompletny
(limit `max_paths` 100 powyżej jego rozmiaru, budżet serwera nie obcięty), każdy łańcuch przechodzi przez transport
dozwolonego rodzaju (`far_end_kinds`), typy obwodów na łańcuchach transportu (`through`: nośniki w L1, obwody sesji w L3;
nośnik nazywa jego `circuit_id`), kierunek odwrotny daje te same łańcuchy, transport trzyma wymagane interfejsy
(`transport_kinds`), zgodność z plikiem wzorcowym (posortowane łańcuchy `rodzaj:hfid`). Para ujemna (`expect_unreachable`):
kompletny pusty wynik w obu warstwach z dokładnym komunikatem serwerowego wyszukiwania. Przy błędzie tabela PASS/FAIL
podaje wynik tracera albo różnicę względem wzorca.

## Katalogi danych, pod, internet, pary

`data:` to katalog próbki (`data/demos/00_full_topology`: numerowane foldery to kolejność ładowania, jedno
`infrahubctl object load`); ładuje go `tests/traversal/load_topology.py` (`async apply(client, branch, force)`; polecenie: `uv run python tests/traversal/load_topology.py --branch main`): ładuje obiekty (pomija, gdy
segment-sentinel jest już na gałęzi, np. odziedziczony z `main` po `test_03`) i wykonuje krok po ładowaniu (wiersz VLAN
drugiego liścia). Te same asercje uruchamia `tests/integration/test_03_full_topology.py` na `main` stosu
(`assert_case` z `check_model.py`, część `invoke test-integration-fast`). Klucze przypadku: `pod` (MLAG, bond
dwunogi, wdrożenie liści i spine do pod, border leaves do DC, wiersze VLAN z odczytu danych; liczba łańcuchów L1 serwer -> krawędź i overlay L3 z obu liści z łańcuchów serwera), `internet` (INTERNET: jeden obiekt pod etykietą Internet z jednym interfejsem, dwa ISP i strony publiczne SaaS i chmury przyłączone do niego; L1 z portu krawędzi przez każdy ISP i INTERNET do portu strony publicznej: jeden łańcuch, trzy nośniki po `circuit_id`; plik wzorcowy `full_topology__internet.txt`; L1 do routera ISP, L3
przez przestrzeń INTERNET, brak tunelu), `transport_sub_interfaces` (port i pakiet transportu widzą swoje peeringi w
`sub_interfaces`); odczyty danych w `tests/traversal/walks.py` (czytają `d` z `graph_read.py`, bez szukania ścieżek), ocena łańcuchów w `tests/traversal/chain_rows.py` (z testami jednostkowymi). `inventory_golden` to to, czego łańcuchy nie pokazują: interfejsy peeringu (zakres, AS peera, ogłaszane prefiksy), bramy z AS, dozwolone prefiksy załączników, interfejsy transportów. Klucze pary: `src`, `dst`, `through`,
`far_end_kinds`, `transport_kinds`, `expect_unreachable`.

## Nowy przypadek

Dopisz wpis do `cases.yml`: katalog schematu, katalogi danych (kolejno), pary `src`/`dst` z oczekiwaniami (`through`, `far_end_kinds`). Tym samym mechanizmem można testować topologię
główną projektu.

## Ograniczenia

- Dane przykładowe już obecne na `main` (np. załadowane ręcznie) gałąź dziedziczy: ładowanie jest pomijane (ponowne
  zdublowałoby zagnieżdżone `segment_deployments`), a test mówi to wprost. Pełny test ładowania plików danych wymaga
  stosu bez tych danych: `--rebuild`.
- Odmowa schematu spowodowana instancjami w innych gałęziach (usunięty rodzaj) wymaga przebudowy stosu: `--rebuild`.
- Nie uruchamia generatorów, transformów ani checków: sprawdza model i dane pisane ręcznie.
- Schemat ładowany jest na gałąź, więc `main` instancji może mieć starszy schemat; asercje dotyczą gałęzi.
