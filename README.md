# Stop-Motion Studio – MVP

Telefon jako kamera → Docker backend → Desktop GUI z onion skinning.

## Architektura

```
[Telefon Android] ──MJPEG:8081──▶ [Docker Backend :8000] ──▶ [Desktop GUI PyQt6]
                                         │
                                    /projects/ (PNG + JSON)
```

## 1. Android – telefon jako kamera

### Wymagania
- Android 8.0+
- Uprawnienia: CAMERA, INTERNET
- Telefon i komputer w tej samej sieci WiFi

### Build
```bash
cd android-app
./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

Po uruchomieniu apki:  
- Wyświetla adres IP: `http://192.168.1.XX:8081/stream`
- Endpointy:
  - `GET /stream` → MJPEG stream (ciągły)
  - `GET /frame`  → pojedyncza klatka JPEG

---

## 2. Docker Backend

### Uruchomienie
```bash
cd docker-backend

# Ustaw IP telefonu:
export PHONE_IP=192.168.1.XX   # <-- adres z ekranu apki

docker-compose up --build
```

Lub bez Docker:
```bash
pip install -r requirements.txt
PHONE_IP=192.168.1.XX uvicorn main:app --host 0.0.0.0 --port 8000
```

### API
| Endpoint | Metoda | Opis |
|---|---|---|
| `/stream` | GET | Proxy MJPEG dla GUI |
| `/frame/take?project=X` | POST | Zapisz klatkę do projektu |
| `/frames?project=X` | GET | Lista klatek + FPS |
| `/frames/{id}?project=X` | DELETE | Usuń klatkę (undo) |
| `/frame/{id}.jpg?project=X` | GET | Pobierz JPEG klatki |
| `/projects` | GET | Lista wszystkich projektów |
| `/projects/{name}/fps` | POST | Ustaw FPS projektu |
| `/config` | GET/POST | Zmień IP telefonu bez restartu |

Swagger UI: `http://localhost:8000/docs`

---

## 3. Desktop GUI (PyQt6)

### Instalacja
```bash
cd desktop-gui
pip install -r requirements.txt
python gui.py
```

### Sterowanie
| Akcja | Klawisz |
|---|---|
| Zapisz klatkę | SPACJA |
| Cofnij ostatnią | Z lub Ctrl+Z |
| Toggle onion skin | przycisk UI |
| Odtwórz animację | ▶ Odtwórz |

### Funkcje
- **Live preview** z telefonu przez MJPEG
- **Onion skinning** – ostatnie 1-4 klatki jako czerwony/niebieski overlay
- **Capture/Undo** – zapisuje do backendu, bufor lokalny do onion skin
- **Odtwarzanie** – frame-by-frame z ustawionym FPS
- **Multi-projekt** – wpisz nazwę projektu w polu
- **FPS per projekt** – zapisywany w `manifest.json`

---

## Struktura projektu (backend)
```
projects/
└── moj-projekt/
    ├── manifest.json          ← {fps, frames: [{id, filename, timestamp}]}
    └── frames/
        ├── 1704000000000_ab12cd34.jpg
        └── ...
```

---

## Sieć – checklist

1. Telefon i komputer w tej samej sieci WiFi (lub hotspot)
2. IP telefonu widoczne na ekranie apki Androida
3. `docker-compose.yml` używa `network_mode: host` – dostęp do telefonu bez NAT
4. Firewall Windows/Mac: odblokuj port 8000 jeśli GUI jest na innym PC

---

## Rozbudowa MVP (kolejne kroki)

- [ ] WebSocket zamiast MJPEG dla mniejszego opóźnienia
- [ ] Export GIF / MP4 przez FFmpeg (`ffmpeg -framerate 12 -i frames/%*.jpg out.gif`)
- [ ] Onion skin w różnych kolorach per warstwa
- [ ] Bluetooth trigger (remote shutter)
- [ ] Tryb klatkowania: wbudowany timer co N sekund
