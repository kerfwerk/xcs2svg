# XCS zu SVG

Kleine Web-App, die Projekte aus **xTool Creative Space / Atomm** (`.xcs`) und **xTool Studio** (`.xs`)
in saubere SVG-Dateien für **LightBurn** umwandelt – in echter Größe (Millimeter), mit einer Farbe pro Ebene.

- Eine SVG pro Arbeitsfläche (Canvas) der Projektdatei.
- **ZIP-Download von Atomm direkt verwenden:** Ein ZIP, das kein xTool-Studio-Projekt ist, wird als Paket
  behandelt – jede enthaltene `.xcs`/`.xs`-Datei wird umgewandelt (eine Ebene tief, verschachtelte ZIPs werden nicht
  entpackt). Die SVGs heißen wie die Projektdatei (z. B. `Giftbox Birthday.svg`), Hinweise stehen mit dem Dateinamen
  davor, eine kaputte Datei im Paket wird nur als Hinweis gemeldet. Andere Dateien (Produktfotos o. Ä.) werden
  übersprungen und in einem Hinweis gezählt. Grenzen: höchstens 50 Projektdateien, 2 000 Einträge, 64 MB
  entpackt und 4 Mio. JSON-Werte für das ganze ZIP.
- **Direkt an LightBurn senden** (LightBurn 2.2, REST-API) – siehe unten.
- `.xs` ist ein ZIP-Archiv aus JSON-Teilen; es wird nur im Arbeitsspeicher und mit fester Obergrenze für die
  entpackte Größe gelesen. Lasereinstellungen kommen aus den Profilen samt Material-Anpassungen.
- Unterstützt: Rechteck (auch mit Eckenradius), Kreis/Ellipse, Linie, Pfad, Zeichenstift-Pfad, Polygon,
  Text (über die in der Datei gespeicherten Glyphen-Pfade) und eingebettete Bilder (PNG, JPEG, GIF, BMP).
- Position, Größe, Drehung, Spiegelung und Scherung werden übernommen.
- Ebenen: Elemente werden nach XCS-Ebene (Ebenenfarbe) bzw. Bearbeitungsart (Schneiden, Linien gravieren,
  Flächengravur …) gruppiert und bekommen die LightBurn-Standardfarben 00 Schwarz, 01 Blau, 02 Rot, 03 Grün …
  Elemente mit unterschiedlichen Lasereinstellungen (Leistung, Geschwindigkeit, Durchgänge, Dichte) landen
  auf getrennten Ebenen; die Werte aus xTool stehen im Ebenennamen, z. B. „Schneiden (100 %, 10 mm/s, 1×)“.
- In xTool gruppierte Elemente bleiben innerhalb ihrer Ebene gruppiert.
- Flächengravuren bleiben gefüllt, Schnitt- und Linienelemente haben keine Füllung.
- Hinweise (z. B. übersprungene Elemente) werden pro Datei angezeigt.
- Dateien werden nur im Arbeitsspeicher verarbeitet, nichts wird gespeichert.

## Starten (Docker oder Podman)

```sh
docker compose up -d --build
```

Mit Podman geht dasselbe mit `podman compose up -d --build`.
Danach im Browser **http://localhost:8080** öffnen, `.xcs`-, `.xs`- oder `.zip`-Dateien hineinziehen und die SVGs
herunterladen (einzeln oder mit „Alle herunterladen“ als ZIP) oder direkt an LightBurn senden.

`compose.yaml` ist die Compose-Datei für beide: `docker compose` und `podman compose` finden sie automatisch.

Die App ist standardmäßig nur auf diesem Rechner erreichbar. Für andere Geräte im Heimnetz in `compose.yaml`
`"127.0.0.1:8080:8080"` durch `"8080:8080"` ersetzen und den Port in der Firewall freigeben.
„An LightBurn senden“ funktioniert von anderen Geräten aus nur, wenn die App per HTTPS erreichbar ist (siehe unten).

Einstellungen (Umgebungsvariablen in `compose.yaml`):

| Variable        | Standard | Bedeutung                              |
| --------------- | -------- | -------------------------------------- |
| `MAX_UPLOAD_MB` | 25       | maximale Dateigröße pro Upload         |
| `MAX_PARALLEL`  | 2        | gleichzeitige Umwandlungen (je bis ca. 350 MB RAM) |
| `MAX_UPLOADS`   | `MAX_PARALLEL` + 2 | gleichzeitig angenommene Uploads |
| `MAX_CONNECTIONS` | 32     | gleichzeitig offene Verbindungen       |
| `PORT`          | 8080     | Port im Container                      |
| `LIGHTBURN_HOSTS` | `host.containers.internal,host.docker.internal` | erlaubte LightBurn-Rechner (Namen oder IP-Adressen, kommagetrennt) |
| `LIGHTBURN_PORTS` | 19520  | erlaubte LightBurn-Ports (LightBurn: 19520, MillMage: 19521) |
| `MAX_SENDS`     | 2        | gleichzeitige Anfragen an LightBurn (an denselben LightBurn immer nur eine Datei zur Zeit) |
| `PUBLIC_HOSTS`  | leer     | weitere Namen, unter denen die App aufgerufen wird (z. B. Domain eines HTTPS-Reverse-Proxys), kommagetrennt |

Der Container läuft gehärtet: Distroless-Image ohne Shell, Benutzer 65532, schreibgeschütztes Dateisystem,
keine Capabilities, Speicher- und Prozesslimit.

Hinweis für `podman build`: Podman übernimmt den `HEALTHCHECK` aus dem Dockerfile nur mit
`--format docker`. `compose.yaml` definiert den Healthcheck deshalb zusätzlich selbst.

## Direkt an LightBurn senden (LightBurn 2.2)

Die App kann fertige SVGs über die REST-API von LightBurn 2.2 direkt ins aktuelle Projekt importieren
(oder das Projekt ersetzen). Einmalige Einrichtung:

1. **Netzwerkzugriff erlauben:** In LightBurn unter *Einstellungen > Erweiterungen (Extensions)*
   **„Allow API Access From Network“** einschalten. Die API hört sonst nur auf dem PC selbst (Loopback);
   der Container läuft aber in der Podman-/Docker-VM und kommt über das virtuelle Netz.
2. **Koppeln:** Auf dem PC mit LightBurn (LightBurn geöffnet)

   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\lightburn-pair.ps1
   ```

   ausführen und den Dialog in LightBurn innerhalb von 30 Sekunden bestätigen. Das Skript zeigt den Schlüssel
   an und kopiert ihn in die Zwischenablage; nach Enter (spätestens nach 60 Sekunden) leert es die Zwischenablage
   wieder (`-NoClipboard`: nur anzeigen). Unter macOS/Linux geht dasselbe mit

   ```sh
   curl -s -X POST http://localhost:19520/api/connect -H 'Content-Type: application/json' -d '{"application_name":"XCS zu SVG","capabilities":["state","upload"]}'
   ```

   (Antwort `{"status":"ok","secret":"…"}`). Alternativ in LightBurn unter *Manage API Security* den Eintrag
   wählen und *Show Secret…* (nur sichtbar, solange der Netzwerkzugriff eingeschaltet ist).
   Der Schlüssel braucht die Berechtigungen `upload` (Senden) und `state` („Verbindung testen“ und die
   Import-Bestätigung). Ein Eintrag ohne `state`, z. B. der eines anderen Programms, kann zwar senden, aber der
   Verbindungstest schlägt dann mit „lehnt den Schlüssel ab“ fehl – am einfachsten mit `tools\lightburn-pair.ps1`
   koppeln, das genau `state` und `upload` anfordert.
3. **Eintragen:** In der App oben rechts **Einstellungen** öffnen (http://localhost:8080/einstellungen),
   Host wählen – Podman: `host.containers.internal`, Docker: `host.docker.internal` –, Port 19520, Modus und
   „Immer gruppiert importieren“ einstellen (aus: es gilt die Gruppier-Einstellung von LightBurn), Schlüssel
   einfügen, **Speichern**, dann **Verbindung testen**.
   Andere Rechner (z. B. die LAN-IP des PCs) in `compose.yaml` bei `LIGHTBURN_HOSTS` ergänzen.
4. **Windows-Firewall:** LightBurn muss eingehende Verbindungen auf TCP-Port 19520 annehmen dürfen. Fragt Windows
   beim Einschalten des Netzwerkzugriffs nach, „Zulassen“ wählen. Wurde früher „Abbrechen“ gewählt, steht in
   *Windows Defender Firewall > Erweiterte Einstellungen > Eingehende Regeln* eine **Blockieren**-Regel für
   LightBurn – sie hat Vorrang und muss auf „Zulassen“ geändert werden. Der Container erreicht den PC über den
   virtuellen WSL-/Hyper-V-Adapter (`vEthernet (WSL …)`, meist eine Adresse aus `172.16.0.0/12`, mit `ipconfig`
   nachsehen); wer die Freigabe eng halten will, erlaubt in der Regel als Remoteadresse nur dieses Netz. Symptom einer blockierenden Firewall:
   „Verbindung testen“ meldet nach 3 Sekunden „antwortet nicht (Zeitüberschreitung)“ statt „Verbindung abgelehnt“.

Danach hat jede fertige SVG den Knopf **„An LightBurn senden“** (bei mehreren zusätzlich „Alle an LightBurn
senden“). Der Status zeigt „in LightBurn importiert“, „übergeben, Bestätigung nicht möglich“ (z. B. Schlüssel ohne
`state` oder Modus „Projekt ersetzen“, für den LightBurn bei SVG kein verlässliches Ergebnis meldet) oder den Fehler.

**Wo liegt der Schlüssel?** Nur in diesem Browser: Beim Speichern wird er als **nicht exportierbarer
WebCrypto-Schlüssel** (HMAC-SHA256) importiert und nur dieses Schlüssel-Objekt in IndexedDB (`xcs2svg` / `settings`)
abgelegt – der Klartext wird nirgends gespeichert (kein localStorage, keine Cookies, kein Server), das Eingabefeld
wird geleert, und selbst Skripte auf der Seite können damit nur signieren, ihn aber nicht auslesen.
**Der Server ist zustandslos:** keine Konfigurationsdatei, kein Volume. Weil LightBurn keine CORS-Header sendet,
reicht der Server die Anfrage nur weiter; er bekommt dabei pro Anfrage ein frisch berechnetes Token
(`hex(HMAC-SHA256(Schlüssel, Unix-Minute))`, von LightBurn nur in der aktuellen und der vorigen Minute akzeptiert),
protokolliert es nicht und gibt es nie zurück. Ziele sind nur die Hosts/Ports aus `LIGHTBURN_HOSTS`/`LIGHTBURN_PORTS`;
fremde Seiten können die Weiterleitung nicht auslösen (eigener Header, Origin-Prüfung und nur unter den
Host-Namen `localhost`, `127.0.0.1`, `[::1]` sowie `PUBLIC_HOSTS` – Schutz vor DNS-Rebinding). Wer die App
hinter einem Reverse-Proxy unter eigener Domain betreibt, trägt diese Domain in `PUBLIC_HOSTS` ein.
„Schlüssel löschen“ entfernt den Eintrag aus dem Browser; widerrufen lässt er sich in LightBurn unter
*Manage API Security*.

Grenzen:

- Die Einstellung gilt **pro Browser und pro Adresse**: `http://localhost:8080` und `http://127.0.0.1:8080` sind für
  den Browser verschiedene Seiten mit getrenntem Speicher. Im privaten Fenster geht sie beim Schließen verloren.
- WebCrypto gibt es nur in einem „sicheren Kontext“: **http://localhost** auf dem PC selbst oder **HTTPS**.
  Über eine LAN-Adresse wie `http://192.168.1.10:8080` ist das Senden abgeschaltet (die Seite erklärt das);
  dafür die App hinter einen HTTPS-Reverse-Proxy stellen.
- Die PC-Uhr muss ungefähr stimmen (Token gilt nur etwa eine Minute).
- An denselben LightBurn geht immer nur eine Datei zur Zeit: LightBurn meldet das Import-Ergebnis ohne Dateinamen,
  gleichzeitige Sendungen ließen sich sonst nicht auseinanderhalten.

## Import in LightBurn

1. **Datei > Importieren** und die SVG-Datei wählen.
2. Jede Farbe landet auf einer eigenen LightBurn-Ebene. Lasereinstellungen (Leistung, Geschwindigkeit,
   Durchgänge) werden **nicht** übernommen und müssen pro Ebene neu eingestellt werden. Die Werte aus
   xTool stehen in der SVG-Datei im Ebenennamen (`data-name` der Ebenen-Gruppe).
3. Gefüllte Flächen auf der Ebene als „Füllen“ einstellen, Konturen als „Linie“.
4. **Größe in mm prüfen** (z. B. ein Rechteck nachmessen). Die SVG enthält echte Millimeter-Angaben.
   Stimmt die Größe trotzdem nicht, in LightBurn unter *Einstellungen* die SVG-Importoptionen
   (automatische Größenerkennung/DPI) prüfen.
5. Die Lage auf der Arbeitsfläche entspricht der in xTool (Ursprung oben links).

## Grenzen und bekannte Lücken

- Lasereinstellungen, Material und Bearbeitungsreihenfolge werden nicht übertragen.
- Text ohne gespeicherte Glyphen-Pfade wird als SVG-Text ausgegeben (mit Hinweis). LightBurn setzt ihn
  dann mit einer eigenen Schrift – besser in xTool vorher in Pfade umwandeln.
- Andere Elementtypen als die oben genannten werden übersprungen und in den Hinweisen gemeldet.
- Scherung wird wie in xTool (PixiJS-Modell) berechnet; bitte bei gescherten Elementen die Form kontrollieren.
- Bilder werden 1:1 eingebettet (keine Graustufen- oder Dithering-Einstellungen).
- Grenzen zum Schutz vor kaputten Dateien: höchstens 20 000 Elemente, 100 Arbeitsflächen,
  1 Mio. Pfadsegmente (200 000 pro Text), 1 Mio. JSON-Objekte, 4 Mio. JSON-Werte, 20 MB pro Bild,
  60 Sekunden pro Umwandlung.
- Uploads, die langsamer als ca. 64 KB/s ankommen, werden abgebrochen.
- Getestet mit XCS-Dateien aus xTool Creative Space 1.2; neuere Dateiversionen können Felder enthalten,
  die noch nicht ausgewertet werden.

## Ohne Docker

Benötigt nur Python 3.11 oder neuer, keine zusätzlichen Pakete.

```sh
python -m app.server            # Web-App auf http://localhost:8080
python -m app.xcs2svg datei.xcs -o ausgabe/   # Kommandozeile
```

## Tests

```sh
python -m unittest discover -s tests -v
```

Echte Beispieldateien liegen **nicht** im Repository (es sind Designs Dritter). Die Tests dafür lesen alle
`.xcs`/`.xs`-Dateien und `Giftbox.zip` (ein Atomm-Download mit `.xs` und Fotos) aus dem Ordner in der
Umgebungsvariable `XCS_FIXTURES`, sonst aus `tests/fixtures/` (von Git ignoriert), und werden übersprungen, wenn
dort nichts liegt. Eigene Beispiele einbinden:

```sh
XCS_FIXTURES=/pfad/zu/meinen/beispielen python -m unittest discover -s tests -v
```

```powershell
$env:XCS_FIXTURES = 'D:\Laser\Beispiele'; python -m unittest discover -s tests -v
```

Die LightBurn-Anbindung wird gegen eine Nachbildung der LightBurn-API getestet (`tests/mock_lightburn.py`, prüft
das Token und die Berechtigungen wie LightBurn). Die Nachbildung lässt sich auch allein starten, z. B.
`python tests/mock_lightburn.py --port 19520 --secret test1234`.

## Hinweise / Rechtliches

- Dieses Projekt ist ein unabhängiges Hobby-Werkzeug. Es steht in keiner Verbindung zu **LightBurn Software** oder
  **xTool** und wird von ihnen weder unterstützt noch geprüft.
- „LightBurn“, „MillMage“, „xTool“, „xTool Creative Space“, „xTool Studio“ und „Atomm“ sind Namen bzw. Marken
  ihrer jeweiligen Inhaber; sie werden hier nur genannt, um zu beschreiben, mit welchen Programmen und Dateien die
  App zusammenarbeitet.
- Die Anbindung nutzt die REST-API von LightBurn. Ihre Nutzung unterliegt der
  [LightBurn API EULA](https://github.com/LightBurnSoftware/LightBurn-REST/blob/main/API-EULA.md) und setzt eine
  gültige LightBurn-Lizenz voraus.
- **Sicherheit:** Die App überträgt nur Zeichnungen an LightBurn. Sie startet nie einen Auftrag und bewegt den
  Laser nicht. Vor jedem Lauf in LightBurn Ebenen, Leistung, Geschwindigkeit, Größe und Position prüfen – die
  Lasereinstellungen aus xTool werden nicht übernommen. Den Laser nie unbeaufsichtigt laufen lassen.
- Beispieldateien zum Testen sind nicht enthalten (Designs Dritter); siehe „Tests“ für eigene Beispiele.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
