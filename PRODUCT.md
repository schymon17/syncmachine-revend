# Product

## Register

product

## Users

Technicy i operatorzy maszyn Revend, ktorzy uruchamiaja lub diagnozuja synchronizator przy automacie. Pracuja w trybie zadaniowym: chca szybko zobaczyc, czy baza maszyny dziala, czy maszyna ma identyfikator, czy jest internet, czy watcher zbiera zdarzenia i kiedy ostatnio wyszedl heartbeat.

## Product Purpose

Sync New Machine jest lokalnym panelem kontroli dla synchronizacji maszyny z API Revend. Ma zastapic uruchamianie skryptow w terminalu, pokazac stan systemu wprost i pozwolic bezpiecznie wykonac podstawowe akcje: zapis API URL, instalacje watcherow, start/stop sync, heartbeat i generowanie danych testowych.

## Brand Personality

Techniczne, spokojne, konkretne. Interfejs ma dawac poczucie kontroli i pewnosci, nie wygladac jak dekoracyjny dashboard marketingowy.

## Anti-references

Nie powinien wygladac jak pusta strona narzedziowa, terminal udajacy aplikacje ani przeozdobiony SaaS dashboard. Unikamy przesadnych gradientow, identycznych kafelkow bez hierarchii i ukrywania waznych statusow w logach.

## Design Principles

1. Najpierw stan systemu, potem akcje.
2. Statusy maja byc czytelne z daleka: OK, blad, ostrzezenie, brak danych.
3. Baza maszyny jest stala i ma byc pokazywana jako fakt, nie jako formularz do przypadkowej edycji.
4. Akcje serwisowe maja byc jasne, spokojne i odwracalne tam, gdzie to mozliwe.
5. Logi uzupelniaja dashboard, ale nie sa glownym sposobem rozumienia aplikacji.

## Accessibility & Inclusion

Cel: czytelny kontrast, brak informacji opartej tylko na kolorze, duze targety klikniecia, przewidywalne etykiety i ograniczony ruch. Panel musi pozostac uzyteczny na laptopie serwisowym i przy slabszych warunkach oswietlenia.
