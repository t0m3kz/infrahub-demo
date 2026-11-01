# Szybki test modelu (traversal)

Zamiast godziny testów integracyjnych: schemat + mały model na jednorazowej gałęzi, potem asercje na przejściu po
grafie (L1: kable, porty, patch panele, obwody nośników; L3: SVI/VRF, peeringi BGP, obwody sesji, interfejs peeringu
chmury, brama, hub, sieć wirtualna, segment chmurowy) w obu kierunkach. Cel: kilka minut.

```bash
uv run invoke check-model                      # wszystkie przypadki z cases.yml
uv run invoke check-model --case cloud_saas_model
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

Asercje na parę: wszystkie rodzaje obiektów modelu niepuste, żaden przeskok oznaczony jako brakujący, L1 i L3 docierają
do segmentu chmurowego, liczba nóg wg typu obwodu (`l1_legs`, `l3_legs` w `cases.yml`), kierunek odwrotny (peeringi
lądujące w sieci wirtualnej segmentu chmurowego = te, do których dochodzi segment DC), zgodność z plikiem wzorcowym
(posortowane listy przeskoków, więc kolejność odpowiedzi API nie ma znaczenia). Przy błędzie tabela PASS/FAIL podaje
przeskok, który się urwał, albo różnicę względem wzorca.

## Nowy przypadek

Dopisz wpis do `cases.yml`: katalog schematu, katalogi danych (kolejno), obiekt `sentinel` (rodzaj i nazwa obiektu,
który istnieje tylko po załadowaniu danych), moduł z funkcjami `load(client, branch)` i
`main_walk(d, dc, cloud, quiet)` oraz pary segmentów z oczekiwaniami. Tym samym mechanizmem można testować topologię
główną projektu.

## Ograniczenia

- Dane przykładowe już obecne na `main` (np. załadowane ręcznie) gałąź dziedziczy: ładowanie jest pomijane (ponowne
  zdublowałoby zagnieżdżone `segment_deployments`), a test mówi to wprost. Pełny test ładowania plików danych wymaga
  stosu bez tych danych: `--rebuild`.
- Odmowa schematu spowodowana instancjami w innych gałęziach (usunięty rodzaj) wymaga przebudowy stosu: `--rebuild`.
- Nie uruchamia generatorów, transformów ani checków: sprawdza model i dane pisane ręcznie.
- Schemat ładowany jest na gałąź, więc `main` instancji może mieć starszy schemat; asercje dotyczą gałęzi.
