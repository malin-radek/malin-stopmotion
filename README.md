# Stop-Motion Studio

Telefon z aplikacja Android rejestruje material, backend w Dockerze synchronizuje konta/projekty/klatki, a GUI webowe jest wbudowane w backend pod `http://localhost:8000/`.

## Architektura

```
[Android/Kotlin]
  - lokalne projekty i material offline
  - mini cache do onion skin
  - sync po odzyskaniu backendu
        |
        | /pairings/claim, /mobile/sync
        v
[Docker Backend/FastAPI]
  - konta, projekty, telefony, klatki
  - QR pairing
  - render MP4 przez ffmpeg
        |
        v
[GUI web w kontenerze]
  - konta i projekty
  - parowanie telefonu QR
  - capture przez legacy stream albo material zsynchronizowany z telefonu
  - render koncowego filmu
```

Stary desktopowy PyQt GUI zostaje w `desktop-gui/`, ale aktualny panel administracyjny jest serwowany bezposrednio przez backend.

## Backend + GUI w Dockerze

```bash
cd docker-backend
docker-compose up --build
```

Otworz:

- GUI: `http://localhost:8000/`
- API: `http://localhost:8000/docs`

Material jest trzymany w `docker-backend/projects`:

```
projects/
  accounts.json
  pairings.json
  <account-id>/
    <project-id>/
      manifest.json
      frames/
      thumbs/
      exports/
```

## Konta, projekty i parowanie

1. W GUI utworz konto albo wybierz `Default`.
2. Utworz projekt, ustaw FPS, rozdzielczosc i tryb `landscape`/`portrait`.
3. Kliknij `Paruj telefon`.
4. Zeskanuj QR telefonem. QR zawiera link `stopmotion://pair?token=...`.
5. Aplikacja Android przypisze telefon do konta i pobierze liste projektow.

Ten sam projekt moze miec wiele sparowanych telefonow. Telefon wysyla pelny material do backendu, a miniatury sa tylko cachem do onion i wydajnego przegladania.

## Android

Projekt Android:

```bash
cd android-app
./gradlew assembleDebug
```

Repo zawiera konfiguracje Gradle, ale nie zawiera wrappera `gradlew`; jesli go brakuje lokalnie, otworz `android-app` w Android Studio albo dodaj wrapper standardowym `gradle wrapper`.

Funkcje aplikacji:

- wybor projektow przypisanych do sparowanego konta,
- tworzenie projektu bez GUI i bez stalego polaczenia z backendem,
- zapis pelnych JPEG lokalnie, gdy backend jest niedostepny,
- miniatury lokalne do onion skin,
- poruszanie onion po klatkach,
- usuniecie biezacej klatki albo wszystkich od biezacej do konca,
- ostrzezenie przy fotografowaniu pionowo w projekcie poziomym,
- synchronizacja materialu po powrocie backendu.

## Najwazniejsze endpointy

| Endpoint | Metoda | Opis |
|---|---|---|
| `/accounts` | GET/POST | Lista i tworzenie kont |
| `/accounts/{id}/projects` | GET/POST | Projekty konta |
| `/accounts/{id}/pairing` | POST | Token i QR do parowania |
| `/pairings/claim` | POST | Telefon przypisuje sie do konta |
| `/mobile/sync` | POST | Synchronizacja projektow i klatek z telefonu |
| `/projects/{id}/render` | POST | Render MP4 w backendzie |
| `/stream` | GET | Legacy MJPEG proxy |
| `/frame/take` | POST | Legacy capture z aktualnego streamu |

## Uwagi

- Priorytet synchronizacji ma material z telefonu. Backend nie nadpisuje klatki o tym samym `local_id`, tylko traktuje ja jako juz przyjeta.
- Cache miniaturek moze byc odbudowany z materialu; nie jest traktowany jako zrodlo prawdy.
- Finalny film powstaje w GUI/backendzie przez `ffmpeg`, nie na telefonie.
